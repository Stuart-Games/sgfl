"""save_v2 — orchestrates the projection_v2 save flow against a real project.

1. Reads <project>/assets.json and <project>/default.project.json.
2. Builds the projection config (entries + tier, Rojo-mounted exclusion paths,
   sidecar coverage, anchor props) and injects it into projection_v2.luau.
3. Runs the script as a Luau execution task with binary output.
4. Unpacks the returned container and writes each entry file into the project
   (only entry-named files are ever written; nothing else in asset folders is
   touched).
5. Downloads the exact place version the task saw, runs the binary sidecar
   extractor, validates the decoder against the in-session anchor values, and
   appends a [!sidecar .] block to the Workspace-owning entry.

Usage:
    py save_v2.py --project C:/path/to/VesselBuilder [--dry-run]
"""

import argparse
import base64
import json
import os
import struct
import sys
import time

import requests

from download_place import downloadPlace, loadDotEnv
from sidecar_extract import ALLOWLIST, ANCHORS, extract

BASE = "https://apis.roblox.com/cloud/v2"
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 300

# entries projected as engine blobs rather than text (overridable per-entry
# with a "format" key in assets.json)
BLOB_DEFAULT_NAMES = {"Workspace", "ReplicatedAssets", "ServerAssets", "ShipModules"}

ANCHOR_PROPS = sorted({prop for cls, prop in ANCHORS if cls == "Workspace"})


def getExecutionKey():
    key = os.environ.get("SGFL_EXECUTION_KEY")
    if key:
        return key
    keyPath = os.path.join(os.path.expanduser("~"), ".sgfl", "execution.key")
    if os.path.isfile(keyPath):
        with open(keyPath, "r", encoding="utf-8") as f:
            return f.read().strip()
    sys.exit("ERROR: no API key (SGFL_EXECUTION_KEY or ~/.sgfl/execution.key)")


def rojoMountedPaths(projectJson):
    """default.project.json tree -> ["Service/Child/...", ...] for every $path mount."""
    paths = []

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


def buildConfig(projectDir):
    with open(os.path.join(projectDir, "assets.json"), "r", encoding="utf-8") as f:
        assets = json.load(f)
    projectPath = os.path.join(projectDir, "default.project.json")
    rojoPaths = []
    if os.path.isfile(projectPath):
        with open(projectPath, "r", encoding="utf-8") as f:
            rojoPaths = rojoMountedPaths(json.load(f))

    entries = []
    for name, spec in assets.items():
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
        "sidecarProps": sorted(f"{cls}.{prop}" for cls, prop in ALLOWLIST if (cls, prop) not in ANCHORS),
        "anchorProps": ANCHOR_PROPS,
    }


def runProjectionTask(scriptSource, universeId, placeId, apiKey, version=None):
    """Create the task with binary output, poll to completion, return (task, containerBytes)."""
    headers = {"x-api-key": apiKey}
    placePath = f"universes/{universeId}/places/{placeId}"
    if version is not None:
        placePath += f"/versions/{version}"
    createUrl = f"{BASE}/{placePath}/luau-execution-session-tasks"
    response = requests.post(
        createUrl,
        headers=headers,
        json={"script": scriptSource, "timeout": "300s", "enableBinaryOutput": True},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        sys.exit(f"ERROR: create task failed HTTP {response.status_code}: {response.text[:2000]}")
    task = response.json()
    taskPath = task["path"]
    print(f"Task created: {taskPath}")

    while task["state"] in ("QUEUED", "PROCESSING"):
        time.sleep(POLL_SECONDS)
        response = requests.get(f"{BASE}/{taskPath}", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        task = response.json()
        print(f"  state: {task['state']}")

    logsResponse = requests.get(f"{BASE}/{taskPath}/logs", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    if logsResponse.ok:
        for chunk in logsResponse.json().get("luauExecutionSessionTaskLogs", []):
            for message in chunk.get("messages", []):
                print(f"  [log] {message}")

    if task["state"] != "COMPLETE":
        sys.exit(f"ERROR: task ended in state {task['state']}: {json.dumps(task.get('error', {}))}")

    binaryOutputUri = task.get("binaryOutputUri")
    if not binaryOutputUri:
        sys.exit("ERROR: task completed but returned no binaryOutputUri")
    downloadResponse = requests.get(binaryOutputUri, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    if downloadResponse.status_code >= 400:
        sys.exit(f"ERROR: binary output download failed HTTP {downloadResponse.status_code}")
    return task, downloadResponse.content


def unpackContainer(container):
    manifestLen = struct.unpack_from("<I", container, 0)[0]
    manifest = json.loads(container[4 : 4 + manifestLen].decode("utf-8"))
    dataStart = 4 + manifestLen
    files = {}
    for entry in manifest["files"]:
        start = dataStart + entry["offset"]
        files[entry["name"]] = container[start : start + entry["size"]]
    return manifest, files


def pythonNumber(value):
    """Match the projection's number semantics for comparison (floats widened from f32)."""
    return value


def renderSidecarValue(typeId, value):
    if typeId == 0x01:
        return 'Base64("' + base64.b64encode(value).decode("ascii") + '")'
    if typeId == 0x02:
        return "true" if value else "false"
    if typeId in (0x03, 0x12):
        return str(int(value))
    return repr(float(value))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project directory (assets.json, .env, ...)")
    parser.add_argument("--dry-run", action="store_true", help="run the task but do not write project files")
    parser.add_argument("--keep", help="also save the raw container to this path")
    parser.add_argument("--out-dir", help="write files here instead of the project directory")
    parser.add_argument("--version", help="pin the projection to a specific place version (default: latest)")
    args = parser.parse_args()

    projectDir = os.path.abspath(args.project)
    env = loadDotEnv(os.path.join(projectDir, ".env"))
    universeId, placeId = env.get("UNIVERSE_ID"), env.get("PLACE_ID")
    if not universeId or not placeId:
        sys.exit("ERROR: project .env must contain UNIVERSE_ID and PLACE_ID")

    config = buildConfig(projectDir)
    print(f"Entries: {', '.join(e['name'] + '(' + e['format'] + ')' for e in config['entries'])}")
    print(f"Rojo-mounted exclusions: {', '.join(config['rojoPaths']) or '(none)'}")

    scriptPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projection_v2.luau")
    with open(scriptPath, "r", encoding="utf-8") as f:
        scriptSource = f.read()
    configJson = json.dumps(config)
    if "__SGFL_CONFIG__" not in scriptSource:
        sys.exit("ERROR: projection_v2.luau is missing the __SGFL_CONFIG__ placeholder")
    scriptSource = scriptSource.replace("__SGFL_CONFIG__", configJson)

    apiKey = getExecutionKey()
    task, container = runProjectionTask(scriptSource, universeId, placeId, apiKey, args.version)
    if args.keep:
        with open(args.keep, "wb") as f:
            f.write(container)

    manifest, files = unpackContainer(container)
    placeVersion = manifest["placeVersion"]
    print(f"\nProjected place version {placeVersion}: {len(files)} files")
    for warning in manifest.get("warnings", []):
        print(f"  WARN(task): {warning}")

    results = task.get("output", {}).get("results", [])
    anchors = results[0].get("anchors", {}) if results else {}

    # dynamic sidecar coverage: every hidden serialized prop the projection
    # found on a service-level entry root, plus the static allowlist
    hiddenRoots = manifest.get("hiddenRoots", {})
    dynamicAllowlist = dict(ALLOWLIST)
    for entryName, info in hiddenRoots.items():
        for propName in info["props"]:
            dynamicAllowlist.setdefault((info["className"], propName), None)
    # service-root attribute blobs (carry CoreScript-gated RBX_* migration
    # state byte-exact; root attributes are excluded from the text projection)
    for entry in config["entries"]:
        if "." not in entry["robloxPath"]:
            dynamicAllowlist.setdefault((entry["robloxPath"], "AttributesSerialize"), 0x01)

    # --- sidecar: download the exact version the task saw and extract ---
    print(f"\nDownloading place version {placeVersion} for sidecar extraction...")
    placeBytes, _ = downloadPlace(placeId, placeVersion)
    tmpPath = os.path.join(projectDir, f".sgfl-sidecar-{placeVersion}.rbxl.tmp")
    with open(tmpPath, "wb") as f:
        f.write(placeBytes)
    try:
        extracted, problems = extract(tmpPath, dynamicAllowlist)
    finally:
        os.remove(tmpPath)
    for problem in problems:
        # services with no attributes simply have no chunk — not a problem
        if "AttributesSerialize: not found" in problem:
            continue
        print(f"  WARN(sidecar): {problem}")

    # anchor canary: extracted binary values must match in-session values
    canaryFailures = []
    for cls, prop in sorted(ANCHORS):
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
        for failure in canaryFailures:
            print(f"  CANARY FAIL: {failure}")
        sys.exit("ERROR: sidecar decoder canary failed — refusing to write sidecar data")
    print(f"Sidecar canary: {len(ANCHORS)} anchors match bit-exactly")

    # append a [!sidecar .] block to every service-level entry whose root class
    # has extracted hidden state (static allowlist targets + dynamic hidden props)
    for entry in config["entries"]:
        if "." in entry["robloxPath"]:
            continue
        rootClass = entry["robloxPath"]
        sidecarLines = []
        for (cls, prop), extractedEntry in sorted(extracted.items()):
            if cls != rootClass or (cls, prop) in ANCHORS:
                continue
            values = extractedEntry["values"]
            if len(values) != 1:
                print(f"  WARN: {cls}.{prop} has {len(values)} instances; sidecar expects a singleton — skipped")
                continue
            sidecarLines.append(f"{prop} = {renderSidecarValue(extractedEntry['typeId'], values[0])}")
        if not sidecarLines:
            continue
        textName = f"{entry['folder']}/{entry['name']}.sgfl"
        if textName in files:
            block = "[!sidecar .]\n" + "\n".join(sidecarLines)
            files[textName] = files[textName].rstrip(b"\n") + b"\n\n" + block.encode("utf-8") + b"\n"
        else:
            print(f"  WARN: no projected text file {textName} to attach sidecar block to")

    # --- write files ---
    if args.dry_run:
        print("\n--dry-run: files NOT written:")
        for name, data in sorted(files.items()):
            print(f"  {name} ({len(data)} bytes)")
        return

    print()
    outDir = os.path.abspath(args.out_dir) if args.out_dir else projectDir
    for name, data in sorted(files.items()):
        target = os.path.join(outDir, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        print(f"  wrote {name} ({len(data)} bytes)")
    print(f"\nSave complete (place version {placeVersion}).")


if __name__ == "__main__":
    main()
