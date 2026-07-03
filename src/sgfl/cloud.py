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
import struct
import time
from typing import Optional

import requests

from . import sidecar
from .util import (
    SGFLError,
    announceStep,
    color,
    getAbsoluteFileURI,
    getFileURI,
    maskSecret,
)

CLOUD_BASE = "https://apis.roblox.com/cloud/v2"
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
TRANSFER_TIMEOUT_SECONDS = 300
TASK_TIMEOUT = "300s"

LEGACY_EXECUTION_KEY_PATH = os.path.join(os.path.expanduser("~"), ".sgfl", "execution.key")

# Entries projected as engine blobs rather than text unless assets.json says
# otherwise (per-entry "format": "text" | "blob" key)
BLOB_DEFAULT_NAMES = {"Workspace", "ReplicatedAssets", "ServerAssets", "ShipModules"}


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


def _cloudRequest(method: str, url: str, *, keyName: str, key: str, step: str, suggestions: list[str], **kwargs):
    try:
        response = requests.request(
            method,
            url,
            headers={"x-api-key": key, **kwargs.pop("headers", {})},
            timeout=kwargs.pop("timeout", REQUEST_TIMEOUT_SECONDS),
            **kwargs,
        )
    except requests.RequestException as exc:
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

    while task["state"] in ("QUEUED", "PROCESSING"):
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
        downloadResponse = requests.get(binaryOutputUri, timeout=TRANSFER_TIMEOUT_SECONDS)
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
    try:
        fileResponse = requests.get(location, timeout=TRANSFER_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise SGFLError("Place file download failed (network error).", details=str(exc))
    if fileResponse.status_code >= 400:
        raise SGFLError(
            "Place file download failed.",
            details=f"HTTP {fileResponse.status_code}",
            suggestions=["Retry — the signed download URL may have expired."],
        )
    return fileResponse.content


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


def buildProjectionConfig(assetTable: dict) -> dict:
    """Build the config JSON injected into both Luau scripts."""
    rojoPaths: list[str] = []
    projectPath = getFileURI("default.project.json")
    if os.path.exists(projectPath):
        with open(projectPath, "r", encoding="utf-8") as f:
            rojoPaths = rojoMountedPaths(json.load(f))

    entries = []
    for name, spec in assetTable.items():
        entryFormat = spec.get("format") or ("blob" if name in BLOB_DEFAULT_NAMES else "text")
        entries.append(
            {
                "name": name,
                "folder": spec["folder"],
                "robloxPath": spec["robloxPath"],
                "format": entryFormat,
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

    announceStep(f"Writing {len(files)} entry files.")
    for name, data in sorted(files.items()):
        target = getFileURI(name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)

    return placeVersion


# ---------------------------------------------------------------------------
# Publish direction
# ---------------------------------------------------------------------------


def collectEntryFiles(assetTable: dict) -> list[tuple[str, bytes]]:
    files = []
    for name, spec in assetTable.items():
        for suffix in (".sgfl", ".sgfl.rbxm"):
            path = getFileURI(f"{spec['folder']}/{name}{suffix}")
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    files.append((f"{spec['folder']}/{name}{suffix}", f.read()))
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


def buildFinalPlace(
    *,
    universeId: str,
    basePlaceId: str,
    executionKey: str,
    publishKey: str,
    downloadKey: str,
    assetTable: dict,
    placeFilePath: str,
) -> bytes:
    """Publish-direction core: take the rojo-built Place.rbxl, apply every
    entry inside an execution session, and return the final place bytes
    (sidecar-patched, ready to upload anywhere).

    Only Saved versions are created on `basePlaceId`; publishing the returned
    bytes is the caller's decision.
    """
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

    entryFiles = collectEntryFiles(assetTable)
    if not entryFiles:
        raise SGFLError(
            "No .sgfl entry files found to publish.",
            suggestions=[
                "Run sgfl save to project the current place into entry files.",
                "If this project still has legacy .rbxm assets, run sgfl migrate first.",
            ],
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

    savedVersion = baseVersion + 1
    announceStep(f"Downloading applied place (version {savedVersion}) for sidecar patch.")
    finalBytes = downloadPlaceFile(basePlaceId, downloadKey, savedVersion)

    patches = parseSidecarBlocks(assetTable)
    for item in applyResult.get("patchProps") or []:
        # props the session could read at save time but not write at apply
        # time (capability asymmetry) come back for binary patching
        value = item["value"]
        if isinstance(value, str):
            value = value.encode("utf-8")
        patches[(item["className"], item["prop"])] = [value]

    if patches:
        patched, applied, problems = sidecar.patchFile(finalBytes, patches)
        for problem in problems:
            print(f"{color.YELLOW}{color.BOLD}WARN{color.END} sidecar patch: {problem}")
        if len(applied) < len(patches):
            missing = sorted(f"{c}.{p}" for c, p in set(patches) - applied)
            raise SGFLError(
                "Some sidecar properties could not be patched — aborting before upload.",
                details="Unpatched: " + ", ".join(missing),
                suggestions=["Run sgfl save to refresh entry files, then retry."],
            )
        announceStep(f"Patched {len(applied)} sidecar properties into the final place file.")
        finalBytes = patched

    return finalBytes
