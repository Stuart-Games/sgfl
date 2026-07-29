"""Open Cloud Luau Execution Session pipeline — the serialization layer.

The engine itself is the only serializer sgfl trusts: a Luau task running in a
cloud execution session projects each assets.json entry into deterministic
text (.sgfl) or an engine-written blob (.sgfl.rbxm), and the publish direction
replays those files inside a session and persists with SavePlaceAsync. State
that cannot cross the Luau boundary rides the binary sidecar (see sidecar.py).

No rbx-dom anywhere: nothing here breaks when Roblox changes its binary
format, because the engine that owns the format does all the reading/writing.

Key facts this module is built around (validated July 2026):
- Task creation is rate-limited to 5/min per API key owner -> one task per
  save and one per publish, with all data in binary input/output.
- The unversioned asset-delivery route returns the LATEST version (saved or
  published) -> versions are always pinned explicitly.
- SavePlaceAsync from a task pinned to version B creates version B+1.
"""

import json
import os
import re
import struct
import sys
import time
from typing import Optional

import requests

from . import sidecar
from .util import (
    SGFLError,
    announceStep,
    color,
    confirmToggle,
    getAbsoluteFileURI,
    getFileURI,
    maskSecret,
)

CLOUD_BASE = "https://apis.roblox.com/cloud/v2"
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
TRANSFER_TIMEOUT_SECONDS = 300
TASK_TIMEOUT = "300s"

# Transient-failure handling. A publish that has already uploaded its Saved
# base version must not die because one packet went missing mid-poll.
MAX_RETRIES = 4
# 5s, 10s, 20s, 40s — 75s cumulative, so a retry sequence can outlast the
# 1-minute window of the 5-task-creations-per-minute limit.
RETRY_BASE_DELAY_SECONDS = 5
RETRY_MAX_DELAY_SECONDS = 60
# Ceiling on the whole create->COMPLETE poll loop. TASK_TIMEOUT bounds the
# script's own runtime, but a task wedged in QUEUED would otherwise poll
# forever and hang a CI job until the runner is killed.
TASK_POLL_TIMEOUT_SECONDS = 900

LEGACY_EXECUTION_KEY_PATH = os.path.join(os.path.expanduser("~"), ".sgfl", "execution.key")

# Entries projected as engine blobs rather than text unless assets.json says
# otherwise (per-entry "format": "text" | "blob" key)
BLOB_DEFAULT_NAMES = {"Workspace", "ReplicatedAssets", "ServerAssets", "ShipModules"}

SUPPORTED_ASSET_VERSION = 2
KNOWN_ASSET_ENTRY_KEYS = {"folder", "robloxPath", "format", "mode", "include", "exclude"}


def _requestDiagnostics(method: str, url: str, keyName: str, key: str, response=None, error=None) -> list[str]:
    lines = [
        f"request: {method} {url}",
        f"auth: x-api-key set via {keyName} (masked={maskSecret(key)}, len={len(key.strip())})",
    ]
    if error is not None:
        lines.append(f"request error: {type(error).__name__}: {error}")
    if response is not None:
        lines.append(f"status: {response.status_code}")
        body = (response.text or "").strip()
        if body:
            lines.append(f"response body preview: {body[:600]}")
    return lines


def _retryDelay(response, attempt: int) -> float:
    """Backoff for one retry: honor Retry-After when Roblox sends it, else
    exponential (5s, 10s, 20s, ...) capped at RETRY_MAX_DELAY_SECONDS."""
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return max(1.0, min(float(header.strip()), RETRY_MAX_DELAY_SECONDS))
            except ValueError:
                pass
    return min(RETRY_BASE_DELAY_SECONDS * (2**attempt), RETRY_MAX_DELAY_SECONDS)


def _isRetryable(method: str, status: Optional[int], allowUnsafeRetry: bool) -> bool:
    """429 is always safe to retry: the request was rejected before it did
    anything. 5xx and connection errors are only safe when re-running the
    request cannot create a second copy of something — GETs, and calls the
    caller explicitly marks as replayable. A place-version POST that 500s may
    well have created the version, so it is never retried automatically."""
    if status == 429:
        return True
    if not (allowUnsafeRetry or method.upper() == "GET"):
        return False
    return status is None or status in (500, 502, 503, 504)


def _cloudRequest(
    method: str,
    url: str,
    *,
    keyName: str,
    key: str,
    step: str,
    suggestions: list[str],
    retries: int = MAX_RETRIES,
    allowUnsafeRetry: bool = False,
    **kwargs,
):
    headers = {"x-api-key": key, **kwargs.pop("headers", {})}
    timeout = kwargs.pop("timeout", REQUEST_TIMEOUT_SECONDS)

    attempt = 0
    while True:
        response = None
        exc: Optional[requests.RequestException] = None
        try:
            response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        except requests.RequestException as caught:
            exc = caught

        status = response.status_code if response is not None else None
        failed = exc is not None or (status is not None and status >= 400)
        if not failed:
            return response

        if attempt < retries and _isRetryable(method, status, allowUnsafeRetry):
            delay = _retryDelay(response, attempt)
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            print(
                f"{color.YELLOW}{color.BOLD}RETRY{color.END} {step}: {reason} — "
                f"retrying in {delay:.0f}s ({attempt + 1}/{retries})."
            )
            time.sleep(delay)
            attempt += 1
            continue
        break

    if exc is not None:
        raise SGFLError(
            f"{step} failed (network error).",
            details=str(exc),
            suggestions=["Verify internet connectivity and try again.", *suggestions],
            diagnostics={"HTTP diagnostics": _requestDiagnostics(method, url, keyName, key, error=exc)},
        )
    if response.status_code >= 400:
        extra = list(suggestions)
        if response.status_code in (401, 403):
            extra.append(
                "Authorization issue (HTTP 401/403): the API key may have expired or lack the "
                "required scope — check key status, scopes, and allowed IPs in the Creator Dashboard."
            )
        raise SGFLError(
            f"{step} was rejected by Roblox.",
            details=f"HTTP {response.status_code}: {(response.text or '').strip()[:500] or 'no response body'}",
            suggestions=extra,
            diagnostics={"HTTP diagnostics": _requestDiagnostics(method, url, keyName, key, response=response)},
        )
    return response


def _getWithRetry(url: str, *, step: str):
    """Plain GET on a pre-signed URL (no API key), with the same backoff as
    _cloudRequest. Returns the final response — the caller checks its status."""
    attempt = 0
    while True:
        response = None
        try:
            response = requests.get(url, timeout=TRANSFER_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            if attempt >= MAX_RETRIES:
                raise SGFLError(f"{step} failed (network error).", details=str(exc))
        status = response.status_code if response is not None else None
        if response is not None and status is not None and status < 400:
            return response
        if attempt >= MAX_RETRIES or not _isRetryable("GET", status, False):
            return response
        delay = _retryDelay(response, attempt)
        print(
            f"{color.YELLOW}{color.BOLD}RETRY{color.END} {step}: "
            f"{f'HTTP {status}' if status else 'network error'} — retrying in {delay:.0f}s."
        )
        time.sleep(delay)
        attempt += 1


def getExecutionKey() -> str:
    """EXECUTION_KEY from the environment (credentials file already merged),
    with a silent fallback to the legacy ~/.sgfl/execution.key file."""
    key = os.environ.get("EXECUTION_KEY")
    if key and key.strip():
        return key.strip()
    if os.path.isfile(LEGACY_EXECUTION_KEY_PATH):
        with open(LEGACY_EXECUTION_KEY_PATH, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    raise SGFLError(
        "Missing environment variable: EXECUTION_KEY",
        details="The cloud pipeline needs an Open Cloud API key with the Luau Execution Sessions system (read + write).",
        suggestions=[
            "Run sgfl auth login to set EXECUTION_KEY.",
            "Create the key in the Creator Dashboard (Open Cloud -> API Keys) with the "
            "'Luau Execution Sessions' system, read + write, scoped to your universe.",
        ],
    )


def runExecutionTask(
    *,
    script: str,
    universeId: str,
    placeId: str,
    executionKey: str,
    version: Optional[int] = None,
    binaryInputPath: Optional[str] = None,
    enableBinaryOutput: bool = False,
    step: str = "Running cloud task",
) -> dict:
    """Create a Luau execution task, poll to completion, print its logs.

    Returns the final task resource (with 'output'; plus 'binaryOutput' bytes
    attached under that key when enableBinaryOutput is set).
    """
    placePath = f"universes/{universeId}/places/{placeId}"
    if version is not None:
        placePath += f"/versions/{version}"

    body: dict = {"script": script, "timeout": TASK_TIMEOUT}
    if binaryInputPath:
        body["binaryInput"] = binaryInputPath
    if enableBinaryOutput:
        body["enableBinaryOutput"] = True

    announceStep(f"{step} (in-engine, universe {universeId}, place {placeId}"
                 + (f", version {version})." if version is not None else ")."))
    response = _cloudRequest(
        "POST",
        f"{CLOUD_BASE}/{placePath}/luau-execution-session-tasks",
        keyName="EXECUTION_KEY",
        key=executionKey,
        step=step,
        suggestions=[
            "Confirm UNIVERSE_ID and PLACE_ID point to the correct place.",
            "Task creation is limited to 5/minute per key owner — wait a minute if you just ran several commands.",
        ],
        json=body,
    )
    task = response.json()
    taskPath = task["path"]

    deadline = time.monotonic() + TASK_POLL_TIMEOUT_SECONDS
    while task["state"] in ("QUEUED", "PROCESSING"):
        if time.monotonic() > deadline:
            raise SGFLError(
                f"{step} did not finish within {TASK_POLL_TIMEOUT_SECONDS}s.",
                details=f"Task {taskPath} was last seen in state {task['state']}.",
                suggestions=[
                    "Nothing further was uploaded — check the place's version history before re-running.",
                    "Check the Roblox status page; execution sessions may be degraded.",
                ],
                diagnostics={"Task diagnostics": [f"task path: {taskPath}", f"state: {task['state']}"]},
            )
        time.sleep(POLL_SECONDS)
        task = _cloudRequest(
            "GET",
            f"{CLOUD_BASE}/{taskPath}",
            keyName="EXECUTION_KEY",
            key=executionKey,
            step="Polling cloud task",
            suggestions=[],
        ).json()

    try:
        logsResponse = requests.get(
            f"{CLOUD_BASE}/{taskPath}/logs",
            headers={"x-api-key": executionKey},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if logsResponse.ok:
            for chunk in logsResponse.json().get("luauExecutionSessionTaskLogs", []):
                for message in chunk.get("messages", []):
                    print(f"  {color.CYAN}[task]{color.END} {message}")
    except requests.RequestException:
        pass

    if task["state"] != "COMPLETE":
        error = task.get("error", {})
        raise SGFLError(
            f"{step} ended in state {task['state']}.",
            details=f"{error.get('code', 'UNKNOWN')}: {error.get('message', 'no error message')}",
            suggestions=[
                "Read the task log lines above for script-level errors.",
                "Re-run with --detailed for full diagnostics.",
            ],
            diagnostics={"Task diagnostics": [f"task path: {taskPath}", f"state: {task['state']}"]},
        )

    if enableBinaryOutput:
        binaryOutputUri = task.get("binaryOutputUri")
        if not binaryOutputUri:
            raise SGFLError(
                f"{step} completed but returned no binary output.",
                suggestions=["This is an internal pipeline error — report it to maintainers."],
            )
        downloadResponse = _getWithRetry(binaryOutputUri, step="Downloading task binary output")
        if downloadResponse.status_code >= 400:
            raise SGFLError(
                "Downloading the task's binary output failed.",
                details=f"HTTP {downloadResponse.status_code}",
                suggestions=["Re-run the command — the output URI is only valid for 15 minutes."],
            )
        task["binaryOutput"] = downloadResponse.content

    return task


def createBinaryInput(universeId: str, executionKey: str, data: bytes) -> str:
    """Upload `data` as a task binary input; returns the resource path."""
    response = _cloudRequest(
        "POST",
        f"{CLOUD_BASE}/universes/{universeId}/luau-execution-session-task-binary-inputs",
        keyName="EXECUTION_KEY",
        key=executionKey,
        step="Creating task binary input",
        suggestions=["Confirm the EXECUTION_KEY has write scope for Luau Execution Sessions."],
        json={"size": len(data)},
    )
    binaryInput = response.json()
    try:
        uploadResponse = requests.put(
            binaryInput["uploadUri"],
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=TRANSFER_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise SGFLError("Uploading task input data failed (network error).", details=str(exc))
    if uploadResponse.status_code >= 400:
        raise SGFLError(
            "Uploading task input data was rejected.",
            details=f"HTTP {uploadResponse.status_code}",
        )
    return binaryInput["path"]


def downloadPlaceFile(placeId: str, downloadKey: str, version: Optional[int] = None) -> bytes:
    """Two-step asset-delivery download, optionally pinned to a version."""
    url = f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}"
    if version is not None:
        url += f"/version/{version}"
    response = _cloudRequest(
        "GET",
        url,
        keyName="DOWNLOAD_KEY",
        key=downloadKey,
        step="Requesting place download URL",
        suggestions=[
            "Check DOWNLOAD_KEY permissions for asset delivery.",
            "Ensure PLACE_ID refers to an accessible place.",
        ],
    )
    location = response.json().get("location")
    if not location:
        raise SGFLError(
            "Roblox response did not include a download location.",
            suggestions=["Verify DOWNLOAD_KEY has the asset-delivery scope."],
        )
    fileResponse = _getWithRetry(location, step="Downloading place file")
    if fileResponse.status_code >= 400:
        raise SGFLError(
            "Place file download failed.",
            details=f"HTTP {fileResponse.status_code}",
            suggestions=["Retry — the signed download URL may have expired."],
        )
    return fileResponse.content


_VERSION_PATH_PATTERN = re.compile(r"/versions/(\d+)$")

# One page is plenty: we only ever compare against a base version created
# moments ago, so the versions we care about are the newest few.
VERSION_PAGE_SIZE = 50


def listPlaceVersions(placeId: str, downloadKey: str) -> Optional[list[int]]:
    """Version numbers that exist for a place, or None if we could not ask.

    This is the only way to learn which version the apply task's
    SavePlaceAsync produced. SavePlaceAsync returns nothing, and the engine
    does not refresh game.PlaceVersion in-session (verified: a session that
    booted at version 5 and saved still reported 5), so without this the
    caller is reduced to assuming baseVersion + 1.

    Places are assets, so this is the Assets API rather than cloud/v2 — see
    https://devforum.roblox.com/t/add-an-open-cloud-endpoint-for-getting-the-version-of-a-place/3442419
    where staff point at ListAssetVersions for exactly this problem. The
    version number lives in each entry's `path`, as assets/{id}/versions/{n}.

    Returns None rather than raising: an unavailable listing is a downgrade in
    confidence, not a failure. Deciding whether that downgrade is acceptable
    belongs to the caller, which knows whether the place is shared.
    """
    try:
        response = _cloudRequest(
            "GET",
            f"https://apis.roblox.com/assets/v1/assets/{placeId}/versions",
            keyName="DOWNLOAD_KEY",
            key=downloadKey,
            step="Listing place versions",
            suggestions=[],
            params={"maxPageSize": VERSION_PAGE_SIZE},
        )
    except SGFLError as err:
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END} "
            f"Could not list place versions ({err.message.rstrip('.')}). "
            f"Grant DOWNLOAD_KEY the Assets read permission on this universe to "
            f"identify the applied version instead of inferring it."
        )
        return None

    versions = []
    for entry in response.json().get("assetVersions") or []:
        match = _VERSION_PATH_PATTERN.search(entry.get("path") or "")
        if match:
            versions.append(int(match.group(1)))
    return versions or None


# ---------------------------------------------------------------------------
# Container format shared with the Luau scripts:
#   [u32 manifestLen][manifest JSON][file data ...]
# ---------------------------------------------------------------------------


def packContainer(files: list[tuple[str, bytes]]) -> bytes:
    manifestFiles = []
    dataParts = []
    offset = 0
    for name, data in files:
        manifestFiles.append({"name": name, "offset": offset, "size": len(data)})
        dataParts.append(data)
        offset += len(data)
    manifest = json.dumps({"files": manifestFiles}).encode("utf-8")
    return struct.pack("<I", len(manifest)) + manifest + b"".join(dataParts)


def unpackContainer(container: bytes) -> tuple[dict, dict[str, bytes]]:
    manifestLen = struct.unpack_from("<I", container, 0)[0]
    manifest = json.loads(container[4 : 4 + manifestLen].decode("utf-8"))
    dataStart = 4 + manifestLen
    files = {}
    for entry in manifest["files"]:
        start = dataStart + entry["offset"]
        files[entry["name"]] = container[start : start + entry["size"]]
    return manifest, files


# ---------------------------------------------------------------------------
# Project config
# ---------------------------------------------------------------------------


def rojoMountedPaths(projectJson: dict) -> list[str]:
    """default.project.json tree -> ["Service/Child/...", ...] for every $path mount."""
    paths: list[str] = []

    def visit(node, segments):
        if not isinstance(node, dict):
            return
        if "$path" in node and segments:
            paths.append("/".join(segments))
        for key, child in node.items():
            if not key.startswith("$"):
                visit(child, segments + [key])

    visit(projectJson.get("tree", {}), [])
    return paths


def _nameGlobMatches(pattern: str, name: str) -> bool:
    """Mirrors the name-glob branch of matchesFilterPattern in the cloud Luau
    scripts ("*" is the only wildcard; every other character matches
    literally). Always False for a "$ClassName" pattern — that branch needs a
    live instance's class, which isn't available at assets.json-validation
    time."""
    if pattern.startswith("$"):
        return False
    regex = "^" + "".join(".*" if ch == "*" else re.escape(ch) for ch in pattern) + "$"
    return re.match(regex, name) is not None


def normalizeAssetConfig(assetTable: dict) -> dict:
    """Validate + normalize a raw assets.json table.

    Single source of truth for assets.json interpretation: every consumer
    (legacy check, start/save/migrate/publish, buildProjectionConfig) calls
    this exactly once via operations._loadAssetTable and never sees raw
    "$"-prefixed keys or unresolved defaults.

    Adds mode/include/exclude/format/pathSegments to every entry (filling in
    defaults), and enforces:
    - $version must be present (and >= 2) if any entry uses mode, include,
      exclude, or a robloxPath deeper than 2 segments. A $version newer than
      this sgfl understands fails loudly with a "run sgfl update" hint.
    - children-mode folders are exclusive (not shared with any other entry).
    - no two entries' robloxPath overlap (equal, or one a prefix of the
      other) at any depth — unless the shorter (ancestor) entry excludes
      the next path segment via a literal name glob, delegating that
      subtree to the other entry. A "$ClassName" exclude doesn't count:
      it can't be verified without a live instance, so it can't license
      a static delegation guarantee.
    """
    assetTable = dict(assetTable)
    version = assetTable.pop("$version", None)
    if version is not None:
        if not isinstance(version, int) or isinstance(version, bool):
            raise SGFLError(
                "assets.json $version must be an integer.",
                details=f"Got: {version!r}",
            )
        if version > SUPPORTED_ASSET_VERSION:
            raise SGFLError(
                f"assets.json declares $version {version}, which this sgfl does not understand.",
                details=f"This sgfl understands up to $version {SUPPORTED_ASSET_VERSION}.",
                suggestions=["Run sgfl update to upgrade to a version that supports this config."],
            )
        if version < 1:
            raise SGFLError("assets.json $version must be >= 1.", details=f"Got: {version}")

    normalized: dict[str, dict] = {}
    for name, spec in assetTable.items():
        if not isinstance(spec, dict):
            raise SGFLError(f'assets.json entry "{name}" must be an object.')
        unknown = set(spec.keys()) - KNOWN_ASSET_ENTRY_KEYS
        if unknown:
            raise SGFLError(
                f'assets.json entry "{name}" has unknown key(s): {", ".join(sorted(unknown))}.',
                suggestions=[f'Known keys: {", ".join(sorted(KNOWN_ASSET_ENTRY_KEYS))}.'],
            )

        folder = spec.get("folder")
        if not isinstance(folder, str) or not folder.strip():
            raise SGFLError(f'assets.json entry "{name}" is missing a non-empty "folder".')

        robloxPath = spec.get("robloxPath")
        if not isinstance(robloxPath, str) or not robloxPath.strip():
            raise SGFLError(f'assets.json entry "{name}" is missing a non-empty "robloxPath".')
        pathSegments = robloxPath.split(".")
        if any(not segment for segment in pathSegments):
            raise SGFLError(f'assets.json entry "{name}": robloxPath "{robloxPath}" has an empty segment.')

        entryFormat = spec.get("format")
        if entryFormat is not None and entryFormat not in ("text", "blob"):
            raise SGFLError(f'assets.json entry "{name}": format must be "text" or "blob", got {entryFormat!r}.')
        if entryFormat is None:
            entryFormat = "blob" if name in BLOB_DEFAULT_NAMES else "text"

        mode = spec.get("mode", "file")
        if mode not in ("file", "children"):
            raise SGFLError(f'assets.json entry "{name}": mode must be "file" or "children", got {mode!r}.')

        include = spec.get("include", [])
        exclude = spec.get("exclude", [])
        for filterName, filterValue in (("include", include), ("exclude", exclude)):
            if not isinstance(filterValue, list) or not all(isinstance(p, str) for p in filterValue):
                raise SGFLError(f'assets.json entry "{name}": {filterName} must be a list of strings.')

        usesV2Features = mode != "file" or bool(include) or bool(exclude) or len(pathSegments) > 2
        if usesV2Features and (version is None or version < 2):
            raise SGFLError(
                f'assets.json entry "{name}" uses a v2 feature (mode, include/exclude, or a robloxPath '
                f"deeper than 2 segments) but $version 2 is not declared.",
                suggestions=['Add "$version": 2 to the top of assets.json.'],
            )

        normalized[name] = {
            "folder": folder,
            "robloxPath": robloxPath,
            "pathSegments": pathSegments,
            "format": entryFormat,
            "mode": mode,
            "include": include,
            "exclude": exclude,
        }

    folderOwners: dict[str, list[str]] = {}
    for name, spec in normalized.items():
        folderOwners.setdefault(spec["folder"], []).append(name)
    for name, spec in normalized.items():
        if spec["mode"] == "children" and len(folderOwners[spec["folder"]]) > 1:
            others = [n for n in folderOwners[spec["folder"]] if n != name]
            raise SGFLError(
                f'assets.json entry "{name}" is mode="children" but folder "{spec["folder"]}" is also '
                f'used by {", ".join(others)} — children-mode folders must be dedicated.',
            )

    names = sorted(normalized.keys())
    for i, nameA in enumerate(names):
        segmentsA = normalized[nameA]["pathSegments"]
        for nameB in names[i + 1 :]:
            segmentsB = normalized[nameB]["pathSegments"]
            if segmentsA == segmentsB:
                raise SGFLError(
                    f'assets.json entries "{nameA}" and "{nameB}" have overlapping robloxPath '
                    f'("{normalized[nameA]["robloxPath"]}" / "{normalized[nameB]["robloxPath"]}").',
                    suggestions=["Each entry must own a disjoint subtree."],
                )
            shorterName, shorter, longerName, longer = (
                (nameA, segmentsA, nameB, segmentsB)
                if len(segmentsA) < len(segmentsB)
                else (nameB, segmentsB, nameA, segmentsA)
            )
            if longer[: len(shorter)] != shorter:
                continue
            delegatedSegment = longer[len(shorter)]
            ancestorExcludes = normalized[shorterName]["exclude"]
            if not any(_nameGlobMatches(pattern, delegatedSegment) for pattern in ancestorExcludes):
                raise SGFLError(
                    f'assets.json entries "{nameA}" and "{nameB}" have overlapping robloxPath '
                    f'("{normalized[nameA]["robloxPath"]}" / "{normalized[nameB]["robloxPath"]}").',
                    suggestions=[
                        "Each entry must own a disjoint subtree, or delegate the nested subtree: add "
                        f'"{delegatedSegment}" to "{shorterName}"\'s exclude list (a literal name glob, '
                        f'not "$ClassName") to hand that child off to "{longerName}".'
                    ],
                )

    return normalized


def buildProjectionConfig(assetTable: dict) -> dict:
    """Build the config JSON injected into both Luau scripts. `assetTable`
    must already be normalized (see normalizeAssetConfig)."""
    rojoPaths: list[str] = []
    projectPath = getFileURI("default.project.json")
    if os.path.exists(projectPath):
        with open(projectPath, "r", encoding="utf-8") as f:
            rojoPaths = rojoMountedPaths(json.load(f))

    entries = []
    for name, spec in assetTable.items():
        entries.append(
            {
                "name": name,
                "folder": spec["folder"],
                "robloxPath": spec["robloxPath"],
                "format": spec["format"],
                "mode": spec["mode"],
                "include": spec["include"],
                "exclude": spec["exclude"],
            }
        )

    return {
        "entries": entries,
        "rojoPaths": rojoPaths,
        "sidecarProps": sorted(
            f"{cls}.{prop}" for cls, prop in sidecar.ALLOWLIST if (cls, prop) not in sidecar.ANCHORS
        ),
        "anchorProps": sorted({prop for cls, prop in sidecar.ANCHORS if cls == "Workspace"}),
    }


# NOTE: sgfl deliberately has no project-wide "stale entry file" sweep. Files
# outside the exact paths an entry owns are inert — collectEntryFiles reads
# strictly {folder}/{Entry}.sgfl(.rbxm) per declared entry, and apply.luau only
# sees what was shipped — so a leftover from a removed/renamed/moved entry
# cannot affect the place. A project-wide scan cannot tell such a leftover from
# a vendored library, git submodule, sibling game in a monorepo, or a branch
# where the entry does not exist yet, all of which legitimately carry .sgfl
# files this project's assets.json says nothing about. Deleting those is
# unrecoverable for untracked files, so the sweep was removed in v2.4.0.
# The one deletion that IS load-bearing is _sweepChildrenFolders (below):
# children-mode folders are dedicated and every .sgfl in them is shipped and
# applied, so a leftover there resurrects a deleted child.


def loadCloudScript(name: str, config: dict, postapplySource: Optional[str] = None) -> str:
    path = getAbsoluteFileURI(f"lua/cloud/{name}")
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    # Replacement is global, so a placeholder token appearing anywhere else
    # (e.g. mentioned in a comment) would get the payload spliced in twice
    # and corrupt the script — demand exactly one occurrence.
    if source.count("__SGFL_CONFIG__") != 1:
        raise SGFLError(
            f"Cloud script {name} must contain the __SGFL_CONFIG__ placeholder exactly once."
        )
    source = source.replace("__SGFL_CONFIG__", json.dumps(config))
    postapplyCount = source.count("__SGFL_POSTAPPLY__")
    if postapplyCount > 1:
        raise SGFLError(
            f"Cloud script {name} must contain the __SGFL_POSTAPPLY__ placeholder exactly once."
        )
    if postapplyCount == 1:
        if postapplySource is not None:
            source = source.replace(
                "__SGFL_POSTAPPLY__", "(function()\n" + postapplySource + "\nend)()"
            )
        else:
            source = source.replace("__SGFL_POSTAPPLY__", "nil")
    return source


# ---------------------------------------------------------------------------
# Save direction
# ---------------------------------------------------------------------------


def runProjectionSave(
    *,
    universeId: str,
    placeId: str,
    executionKey: str,
    downloadKey: str,
    assetTable: dict,
    version: Optional[int] = None,
) -> int:
    """Full save: project the place into entry files and write them into the
    project. Returns the place version that was projected."""
    config = buildProjectionConfig(assetTable)
    script = loadCloudScript("projection.luau", config)

    task = runExecutionTask(
        script=script,
        universeId=universeId,
        placeId=placeId,
        executionKey=executionKey,
        version=version,
        enableBinaryOutput=True,
        step="Projecting place into entry files",
    )
    manifest, files = unpackContainer(task["binaryOutput"])
    placeVersion = manifest["placeVersion"]
    for warning in manifest.get("warnings", []):
        print(f"{color.YELLOW}{color.BOLD}WARN{color.END} {warning}")

    results = task.get("output", {}).get("results", [])
    anchors = results[0].get("anchors", {}) if results else {}

    # dynamic sidecar coverage: hidden root props reported by the projection,
    # service-root attribute blobs, plus the static allowlist
    dynamicAllowlist = dict(sidecar.ALLOWLIST)
    for info in manifest.get("hiddenRoots", {}).values():
        for propName in info["props"]:
            dynamicAllowlist.setdefault((info["className"], propName), None)
    for entry in config["entries"]:
        if "." not in entry["robloxPath"]:
            dynamicAllowlist.setdefault((entry["robloxPath"], "AttributesSerialize"), 0x01)

    announceStep(f"Extracting binary sidecar from place version {placeVersion}.")
    placeBytes = downloadPlaceFile(placeId, downloadKey, placeVersion)
    extracted, problems = sidecar.extract(placeBytes, dynamicAllowlist)
    for problem in problems:
        if "AttributesSerialize: not found" in problem:
            continue  # services with no attributes simply have no chunk
        print(f"{color.YELLOW}{color.BOLD}WARN{color.END} sidecar: {problem}")

    # anchor canary: the binary decoder must agree with the engine bit-exactly
    canaryFailures = []
    for cls, prop in sorted(sidecar.ANCHORS):
        key = (cls, prop)
        if key not in extracted or prop not in anchors:
            canaryFailures.append(f"{cls}.{prop}: missing ({'binary' if key not in extracted else 'session'})")
            continue
        binaryValue = extracted[key]["values"][0]
        sessionValue = anchors[prop]
        matches = binaryValue == sessionValue or (
            isinstance(binaryValue, float) and float(sessionValue) == binaryValue
        )
        if not matches:
            canaryFailures.append(f"{cls}.{prop}: binary={binaryValue!r} session={sessionValue!r}")
    if canaryFailures:
        raise SGFLError(
            "Sidecar decoder canary failed — the binary format may have changed.",
            details="; ".join(canaryFailures),
            suggestions=[
                "Nothing was written. Update sgfl before saving again.",
                "Report this to maintainers with the failing property names.",
            ],
        )

    # append a [!sidecar .] block to every service-level entry with hidden state
    for entry in config["entries"]:
        if "." in entry["robloxPath"]:
            continue
        rootClass = entry["robloxPath"]
        sidecarLines = []
        for (cls, prop), extractedEntry in sorted(extracted.items()):
            if cls != rootClass or (cls, prop) in sidecar.ANCHORS:
                continue
            values = extractedEntry["values"]
            if len(values) != 1:
                print(
                    f"{color.YELLOW}{color.BOLD}WARN{color.END} "
                    f"{cls}.{prop} has {len(values)} instances; sidecar expects a singleton — skipped"
                )
                continue
            sidecarLines.append(f"{prop} = {sidecar.renderSidecarValue(extractedEntry['typeId'], values[0])}")
        if not sidecarLines:
            continue
        textName = f"{entry['folder']}/{entry['name']}.sgfl"
        if textName in files:
            block = "[!sidecar .]\n" + "\n".join(sidecarLines)
            files[textName] = files[textName].rstrip(b"\n") + b"\n\n" + block.encode("utf-8") + b"\n"

    _guardEmptiedEntries(config["entries"], files, placeVersion)

    announceStep(f"Writing {len(files)} entry files.")
    for name, data in sorted(files.items()):
        target = getFileURI(name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

    _sweepChildrenFolders(config["entries"], files)

    return placeVersion


def _countBlocks(data: bytes) -> int:
    """Number of instance blocks in a .sgfl text projection.

    Block headers ("[path] ClassName", optionally " !blob") are the only
    unindented lines starting with "["; heredoc bodies are tab-prefixed, and
    driver-appended "[!sidecar ...]" markers are not instances."""
    return sum(1 for line in data.split(b"\n") if line.startswith(b"[") and not line.startswith(b"[!"))


def _emptiedEntries(entries: list, files: dict) -> list[str]:
    """Entries whose projection came back empty this save while the committed
    files for that entry still hold content. Returns human-readable labels.

    Only entries that actually projected are considered — one that failed
    writes nothing at all and keeps its files (see _sweepChildrenFolders)."""
    emptied = []
    for entry in entries:
        rootName = f"{entry['folder']}/{entry['name']}.sgfl"
        if rootName not in files:
            continue

        if entry["mode"] == "children":
            prefix = entry["folder"] + "/"
            # direct children only: a nested entry's folder can sit underneath
            # this one and its files must not be counted as this entry's
            newChildren = sum(
                1
                for name in files
                if name.startswith(prefix) and name != rootName and "/" not in name[len(prefix) :]
            )
            if newChildren:
                continue
            folderPath = getFileURI(entry["folder"])
            if not os.path.isdir(folderPath):
                continue
            oldChildren = [
                fileName
                for fileName in os.listdir(folderPath)
                if fileName != f"{entry['name']}.sgfl"
                and (fileName.endswith(".sgfl") or fileName.endswith(".sgfl.rbxm"))
            ]
            if oldChildren:
                emptied.append(f"{entry['name']} ({entry['folder']}/): {len(oldChildren)} child file(s) -> 0")
            continue

        if entry["format"] == "blob":
            blobName = f"{entry['folder']}/{entry['name']}.sgfl.rbxm"
            if blobName in files:
                continue
            if os.path.isfile(getFileURI(blobName)):
                emptied.append(f"{entry['name']} ({blobName}): children blob -> no children")
            continue

        rootPath = getFileURI(rootName)
        if not os.path.isfile(rootPath):
            continue
        with open(rootPath, "rb") as f:
            oldBlocks = _countBlocks(f.read())
        newBlocks = _countBlocks(files[rootName])
        if oldBlocks > 1 and newBlocks <= 1:
            emptied.append(f"{entry['name']} ({rootName}): {oldBlocks} blocks -> {newBlocks}")
    return emptied


def _guardEmptiedEntries(entries: list, files: dict, placeVersion: int) -> None:
    """Confirm before a save that would empty entries which still have content
    on disk. Nothing has been written when this runs, so declining is free.

    The dangerous path this exists for: a `sgfl start`/`publish` that dies after
    uploading the rojo-built base (scripts only, no assets) but before the apply
    task's SavePlaceAsync — a rate-limited task creation, a Luau error, Ctrl+C
    during the multi-minute poll. The place's latest version is then an
    asset-less skeleton, and `sgfl save` (which reads the latest version, saved
    or published) would happily project it over every entry file in the repo.
    Clearing a whole subtree on purpose is rare, so this asks rather than
    refuses; a non-interactive session writes nothing."""
    emptied = _emptiedEntries(entries, files)
    if not emptied:
        return

    noun = "entry" if len(emptied) == 1 else "entries"
    print(
        f"{color.RED}{color.BOLD}STOP{color.END} "
        f"place version {placeVersion} projects {len(emptied)} {noun} as EMPTY, "
        f"but they still have content on disk:"
    )
    for label in emptied:
        print(f"        - {label}")
    print(
        f"{color.YELLOW}{color.BOLD}WARN{color.END} "
        f"If you did not just clear these in Studio, the place's latest version is probably a "
        f"failed build (a `sgfl start`/`publish` that died before applying uploads a scripts-only "
        f"base version). Re-publish first, then save."
    )

    if not sys.stdin.isatty():
        raise SGFLError(
            "Refusing to overwrite entry files with an empty projection in a non-interactive session.",
            details="Emptied: " + "; ".join(emptied),
            suggestions=[
                "Nothing was written.",
                "Re-run from a terminal to confirm, or re-publish the place first.",
            ],
        )

    message = (
        f"{color.RED}{color.BOLD}Write the empty projection over these files?{color.END}"
        f"  (←/→ to choose, Enter to commit)"
    )
    if not confirmToggle(message, defaultYes=False):
        raise SGFLError(
            "Save aborted — entry files left untouched.",
            suggestions=[
                "Re-publish the place (sgfl start) so its latest version has your assets, then save.",
                "If the emptying is intentional, re-run and confirm.",
            ],
        )


def _sweepChildrenFolders(entries: list, writtenFiles: dict) -> None:
    """children-mode folders are dedicated and fully sgfl-managed: any .sgfl /
    .sgfl.rbxm file in the folder that wasn't just written this save is a
    stale leftover (e.g. from a deleted or renamed child) and is removed.
    An entry that failed to project writes nothing this save — its folder is
    skipped so a transient error can't wipe committed child files."""
    for entry in entries:
        if entry["mode"] != "children":
            continue
        if f"{entry['folder']}/{entry['name']}.sgfl" not in writtenFiles:
            continue
        folderPath = getFileURI(entry["folder"])
        if not os.path.isdir(folderPath):
            continue
        written = {name for name in writtenFiles if name.startswith(entry["folder"] + "/")}
        for fileName in os.listdir(folderPath):
            if not (fileName.endswith(".sgfl") or fileName.endswith(".sgfl.rbxm")):
                continue
            relPath = f"{entry['folder']}/{fileName}"
            if relPath in written:
                continue
            os.remove(getFileURI(relPath))
            print(f"{color.YELLOW}{color.BOLD}WARN{color.END} deleted stale unmanaged file: {relPath}")


# ---------------------------------------------------------------------------
# Publish direction
# ---------------------------------------------------------------------------


def _hasCrlfStructuralLines(data: bytes) -> bool:
    """True if any structural line of a .sgfl file ends in \\r (CRLF-converted).

    Heredoc body lines are always tab-prefixed and may legitimately end in a
    raw \\r (string values containing CRLF), so only unindented lines count."""
    for line in data.split(b"\n"):
        if line.endswith(b"\r") and not line.startswith(b"\t"):
            return True
    return False


def collectEntryFiles(assetTable: dict) -> list[tuple[str, bytes]]:
    files = []
    crlfFiles = []

    def addFile(relPath: str, absPath: str) -> None:
        with open(absPath, "rb") as f:
            data = f.read()
        if relPath.endswith(".sgfl") and _hasCrlfStructuralLines(data):
            crlfFiles.append(relPath)
        files.append((relPath, data))

    for name, spec in assetTable.items():
        rootPath = getFileURI(f"{spec['folder']}/{name}.sgfl")
        if os.path.isfile(rootPath):
            addFile(f"{spec['folder']}/{name}.sgfl", rootPath)

        if spec["mode"] == "children":
            folderPath = getFileURI(spec["folder"])
            if not os.path.isdir(folderPath):
                continue
            rootFileName = f"{name}.sgfl"
            for fileName in sorted(os.listdir(folderPath)):
                if fileName == rootFileName:
                    continue
                if not (fileName.endswith(".sgfl") or fileName.endswith(".sgfl.rbxm")):
                    continue
                filePath = os.path.join(folderPath, fileName)
                if not os.path.isfile(filePath):
                    continue
                addFile(f"{spec['folder']}/{fileName}", filePath)
        else:
            blobPath = getFileURI(f"{spec['folder']}/{name}.sgfl.rbxm")
            if os.path.isfile(blobPath):
                addFile(f"{spec['folder']}/{name}.sgfl.rbxm", blobPath)

    if crlfFiles:
        raise SGFLError(
            "Entry files have CRLF line endings — the apply task parses them byte-exact "
            "and would clear subtrees without rebuilding them.",
            details="Affected: " + ", ".join(sorted(crlfFiles)),
            suggestions=[
                "A git checkout with core.autocrlf=true likely converted them (branch switch/merge).",
                "Add a .gitattributes with '*.sgfl text eol=lf' (plus '*.rbxm binary'), then re-checkout:",
                "  git ls-files -z -- '*.sgfl' | xargs -0 rm -- && git checkout -- .",
            ],
        )
    return files


def parseSidecarBlocks(assetTable: dict) -> dict:
    """Read [!sidecar X] blocks from entry .sgfl files -> {(class, prop): [value]}."""
    patches = {}
    for name, spec in assetTable.items():
        if "." in spec["robloxPath"]:
            continue
        rootClass = spec["robloxPath"]
        path = getFileURI(f"{spec['folder']}/{name}.sgfl")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        currentClass = None
        for line in lines:
            if line.startswith("[!sidecar ") and line.endswith("]"):
                target = line[len("[!sidecar ") : -1]
                currentClass = rootClass if target == "." else target
                continue
            if line.startswith("["):
                currentClass = None
                continue
            if currentClass is None or not line.strip():
                continue
            propName, sep, valueText = line.partition(" = ")
            if not sep:
                continue
            patches[(currentClass, propName)] = [sidecar.parseSidecarValue(valueText)]
    return patches


def uploadPlaceVersion(
    *, universeId: str, placeId: str, publishKey: str, data: bytes, versionType: str
) -> int:
    """POST place bytes via the v1 versions endpoint; returns the new version number."""
    response = _cloudRequest(
        "POST",
        f"https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType={versionType}",
        keyName="PUBLISH_KEY",
        key=publishKey,
        step=f"Uploading place ({versionType})",
        suggestions=[
            "Confirm PLACE_ID and UNIVERSE_ID point to the correct place.",
            "Regenerate your PUBLISH_KEY and ensure it has publish permissions.",
        ],
        headers={"Content-Type": "application/octet-stream"},
        data=data,
        timeout=TRANSFER_TIMEOUT_SECONDS,
    )
    return response.json()["versionNumber"]


def _inferredSaveVersion(baseVersion: int, strict: bool, why: str) -> int:
    """Last resort: assume the save took the next number.

    Sound only while nothing else writes to the place during the task, which
    runs for minutes. On a build place shared between repos that assumption can
    land on *another project's* version — and the byte-identity guard in
    buildFinalPlace won't catch it, because it compares against our own base
    bytes and another project's build is not byte-identical to ours. So under
    `strict` this refuses rather than guesses.
    """
    if not strict:
        return baseVersion + 1
    raise SGFLError(
        "Could not determine which version the apply task saved.",
        details=(
            f"Base version {baseVersion}, and {why}. On a build place that other "
            "projects also build against, assuming version "
            f"{baseVersion + 1} could download a different project's place file."
        ),
        suggestions=[
            "Nothing was uploaded. Grant DOWNLOAD_KEY the Assets read permission on "
            "this universe — that lets sgfl read the real version instead of inferring it.",
            "Or give this project its own build place, where the inference is sound "
            "because nothing else writes to it.",
        ],
    )


def _postSaveVersion(
    applyResult: dict,
    baseVersion: int,
    observedVersions: Optional[list[int]] = None,
    *,
    strict: bool = False,
) -> int:
    """Version the apply task's SavePlaceAsync produced, best source first.

    1. The engine's own post-save game.PlaceVersion. Authoritative when present,
       but in practice it is not: the engine does not refresh it in-session, so
       it comes back equal to baseVersion. Kept because it costs nothing and
       becomes correct the day Roblox changes that.
    2. The versions that actually exist on the place (listPlaceVersions).
       Exactly one above base is ours, with certainty. Several means someone
       else really did write during the task — the case `strict` exists for,
       now detected from evidence rather than assumed.
    3. baseVersion + 1.
    """
    reported = applyResult.get("savedVersion")
    if not isinstance(reported, bool) and isinstance(reported, (int, float)):
        reported = int(reported)
        if reported > baseVersion:
            if reported != baseVersion + 1:
                print(
                    f"{color.YELLOW}{color.BOLD}WARN{color.END} "
                    f"Another version landed on this place while the apply task ran "
                    f"(base {baseVersion}, applied {reported}) — using the engine-reported version."
                )
            return reported

    newer = sorted(v for v in (observedVersions or []) if v > baseVersion)
    if len(newer) == 1:
        return newer[0]
    if len(newer) > 1:
        if strict:
            raise SGFLError(
                "More than one place version was created while the apply task ran.",
                details=(
                    f"Base version {baseVersion}; versions {', '.join(str(v) for v in newer)} "
                    "all appeared since. Another project almost certainly built against this "
                    "place at the same time, and nothing identifies which version is ours."
                ),
                suggestions=[
                    "Nothing was uploaded. Re-run the build — collisions are transient.",
                    "If this is frequent, give this project its own build place so it is "
                    "never contending for version numbers.",
                ],
            )
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END} "
            f"Versions {', '.join(str(v) for v in newer)} all landed on this place since "
            f"base {baseVersion} — assuming {baseVersion + 1} is ours."
        )
        return baseVersion + 1

    if observedVersions:
        # The listing worked but shows nothing past base. Most likely the
        # listing is lagging behind the save rather than the save not having
        # happened, so infer rather than declare failure.
        return _inferredSaveVersion(
            baseVersion, strict, "the version listing showed nothing newer than it"
        )
    return _inferredSaveVersion(baseVersion, strict, "the version listing was unavailable")


def buildFinalPlace(
    *,
    universeId: str,
    basePlaceId: str,
    executionKey: str,
    publishKey: str,
    downloadKey: str,
    assetTable: dict,
    placeFilePath: str,
    strictSaveVersion: bool = False,
) -> bytes:
    """Publish-direction core: take the rojo-built Place.rbxl, apply every
    entry inside an execution session, and return the final place bytes
    (sidecar-patched, ready to upload anywhere).

    Only Saved versions are created on `basePlaceId`; publishing the returned
    bytes is the caller's decision.

    Set `strictSaveVersion` when `basePlaceId` is a build place other projects
    also build against: it makes an unreported post-save version fatal instead
    of falling back to the baseVersion + 1 guess. See _postSaveVersion.
    """
    # collect (and line-ending-validate) entry files before any cloud call
    entryFiles = collectEntryFiles(assetTable)
    if not entryFiles:
        raise SGFLError(
            "No .sgfl entry files found to publish.",
            suggestions=[
                "Run sgfl save to project the current place into entry files.",
                "If this project still has legacy .rbxm assets, run sgfl migrate first.",
            ],
        )

    announceStep("Uploading script build as a Saved base version.")
    with open(placeFilePath, "rb") as f:
        baseBytes = f.read()
    baseVersion = uploadPlaceVersion(
        universeId=universeId,
        placeId=basePlaceId,
        publishKey=publishKey,
        data=baseBytes,
        versionType="Saved",
    )

    config = buildProjectionConfig(assetTable)
    postapplySource = None
    postapplyPath = getFileURI("postapply.luau")
    if os.path.isfile(postapplyPath):
        with open(postapplyPath, "r", encoding="utf-8") as f:
            postapplySource = f.read()
        announceStep("Including postapply.luau hook.")
    script = loadCloudScript("apply.luau", config, postapplySource)

    binaryInputPath = createBinaryInput(universeId, executionKey, packContainer(entryFiles))
    task = runExecutionTask(
        script=script,
        universeId=universeId,
        placeId=basePlaceId,
        executionKey=executionKey,
        version=baseVersion,
        binaryInputPath=binaryInputPath,
        step="Applying entry files to the place",
    )
    results = task.get("output", {}).get("results", [])
    applyResult = results[0] if results else {}
    for warning in applyResult.get("warnings", []):
        print(f"{color.YELLOW}{color.BOLD}WARN{color.END} {warning}")
    if applyResult.get("failedRefs"):
        raise SGFLError(
            "Apply left unresolved instance references — aborting before upload.",
            details=f"{applyResult['failedRefs']} refs failed (see warnings above).",
            suggestions=[
                "Check that the referenced instances still exist in the entry files.",
                "Run sgfl save to regenerate entry files from a known-good place version.",
            ],
        )

    # Which version did the session's SavePlaceAsync produce? Nothing hands us
    # that number: SavePlaceAsync returns nothing, and the engine does not
    # refresh game.PlaceVersion in-session. So ask the place what versions it
    # actually has — see listPlaceVersions — and fall back to baseVersion + 1,
    # which holds only while nothing else wrote during the task.
    observedVersions = listPlaceVersions(basePlaceId, downloadKey)
    savedVersion = _postSaveVersion(
        applyResult, baseVersion, observedVersions, strict=strictSaveVersion
    )
    announceStep(f"Downloading applied place (version {savedVersion}) for sidecar patch.")
    finalBytes = downloadPlaceFile(basePlaceId, downloadKey, savedVersion)
    if finalBytes == baseBytes:
        # We got back the unapplied rojo build — assets never landed. Publishing
        # this would strip every asset from the live game.
        raise SGFLError(
            f"Place version {savedVersion} is byte-identical to the unapplied base build.",
            details=(
                "The apply task's saved version could not be identified — most likely another "
                "publish or a Studio save landed on this place while the task was running."
            ),
            suggestions=[
                "Nothing was uploaded. Make sure no one else is publishing this place, then re-run.",
                "Check the place's version history in Studio to see what landed.",
            ],
        )

    patches = parseSidecarBlocks(assetTable)
    for item in applyResult.get("patchProps") or []:
        # props the session could read at save time but not write at apply
        # time (capability asymmetry) come back for binary patching
        value = item["value"]
        if isinstance(value, str):
            value = value.encode("utf-8")
        patches[(item["className"], item["prop"])] = [value]

    if patches:
        patched, applied, problems, omitted = sidecar.patchFile(finalBytes, patches)
        for problem in problems + omitted:
            print(f"{color.YELLOW}{color.BOLD}WARN{color.END} sidecar patch: {problem}")
        # `omitted` properties are not failures. The engine chose not to
        # serialize them, there is no chunk to rewrite and no way to add one,
        # so aborting would leave the project permanently unbuildable while
        # protecting nothing. `problems` still abort.
        unwritable = sorted(f"{c}.{p}" for c, p in set(patches) - applied) if problems else []
        if unwritable:
            raise SGFLError(
                "Some sidecar properties could not be patched — aborting before upload.",
                details="Unpatched: " + ", ".join(unwritable),
                suggestions=["Run sgfl save to refresh entry files, then retry."],
            )
        announceStep(
            f"Patched {len(applied)} sidecar properties into the final place file"
            + (f" ({len(omitted)} not serialized by the engine)." if omitted else ".")
        )
        finalBytes = patched

    return finalBytes
