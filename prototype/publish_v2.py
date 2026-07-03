"""publish_v2 — orchestrates the apply_v2 publish flow against a real project.

1. rojo build -> Place.rbxl (scripts + project tree).
2. Upload as a Saved version (base B) — the live place is untouched.
3. Pack the project's entry files (.sgfl / .sgfl.rbxm) into a binary-input
   container; run apply_v2.luau version-pinned to B (applies every entry,
   runs postapply.luau if present, SavePlaceAsync -> version B+1).
4. Download B+1, patch [!sidecar] values into the binary (script-unreachable
   state), and upload the patched bytes.
5. By default the final upload is versionType=Saved (safe testing). Pass
   --publish for versionType=Published (the real thing).

Usage:
    py publish_v2.py --project C:/path/to/VesselBuilder [--publish] [--no-build]
"""

import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import time

import requests

from download_place import downloadPlace, loadDotEnv
from save_v2 import buildConfig, getExecutionKey
from sidecar_patch import patch_file
from upload_place import uploadPlace

BASE = "https://apis.roblox.com/cloud/v2"
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 300


def packContainer(files):
    manifestFiles = []
    dataParts = []
    offset = 0
    for name, data in files:
        manifestFiles.append({"name": name, "offset": offset, "size": len(data)})
        dataParts.append(data)
        offset += len(data)
    manifest = json.dumps({"files": manifestFiles}).encode("utf-8")
    return struct.pack("<I", len(manifest)) + manifest + b"".join(dataParts)


def collectEntryFiles(projectDir, config):
    files = []
    for entry in config["entries"]:
        for suffix in (".sgfl", ".sgfl.rbxm"):
            name = f"{entry['folder']}/{entry['name']}{suffix}"
            path = os.path.join(projectDir, name.replace("/", os.sep))
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    files.append((name, f.read()))
    return files


def parseSidecarBlocks(projectDir, config):
    """Read [!sidecar X] blocks from entry .sgfl files -> {(class, prop): value}."""
    patches = {}
    for entry in config["entries"]:
        if "." in entry["robloxPath"]:
            continue
        rootClass = entry["robloxPath"]
        path = os.path.join(projectDir, entry["folder"].replace("/", os.sep), entry["name"] + ".sgfl")
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
        currentClass = None
        for line in lines:
            header = re.match(r"^\[!sidecar (.+)\]$", line)
            if header:
                target = header.group(1)
                currentClass = rootClass if target == "." else target
                continue
            if line.startswith("["):
                currentClass = None
                continue
            if currentClass is None or not line.strip():
                continue
            m = re.match(r"^(\S+) = (.*)$", line)
            if not m:
                continue
            patches[(currentClass, m.group(1))] = [parseSidecarValue(m.group(2))]
    return patches


def parseSidecarValue(text):
    if text == "true":
        return True
    if text == "false":
        return False
    m = re.match(r'^Base64\("(.*)"\)$', text)
    if m:
        return base64.b64decode(m.group(1))
    if re.match(r"^-?\d+$", text):
        return int(text)
    return float(text)


def createBinaryInput(headers, universeId, data):
    response = requests.post(
        f"{BASE}/universes/{universeId}/luau-execution-session-task-binary-inputs",
        headers=headers,
        json={"size": len(data)},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        sys.exit(f"ERROR: create binary input failed HTTP {response.status_code}: {response.text[:2000]}")
    binaryInput = response.json()
    uploadResponse = requests.put(
        binaryInput["uploadUri"],
        data=data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=UPLOAD_TIMEOUT_SECONDS,
    )
    if uploadResponse.status_code >= 400:
        sys.exit(f"ERROR: binary input upload failed HTTP {uploadResponse.status_code}")
    return binaryInput["path"]


def runApplyTask(scriptSource, universeId, placeId, versionId, binaryInputPath, apiKey):
    headers = {"x-api-key": apiKey}
    createUrl = f"{BASE}/universes/{universeId}/places/{placeId}/versions/{versionId}/luau-execution-session-tasks"
    body = {"script": scriptSource, "timeout": "300s", "binaryInput": binaryInputPath}
    response = requests.post(createUrl, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        sys.exit(f"ERROR: create apply task failed HTTP {response.status_code}: {response.text[:2000]}")
    task = response.json()
    taskPath = task["path"]
    print(f"Apply task created: {taskPath}")
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
        sys.exit(f"ERROR: apply task ended in state {task['state']}: {json.dumps(task.get('error', {}))}")
    results = task.get("output", {}).get("results", [])
    return results[0] if results else {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--publish", action="store_true", help="final upload as Published (default: Saved)")
    parser.add_argument("--no-build", action="store_true", help="reuse existing Place.rbxl instead of rojo build")
    args = parser.parse_args()

    projectDir = os.path.abspath(args.project)
    env = loadDotEnv(os.path.join(projectDir, ".env"))
    universeId, placeId = env.get("UNIVERSE_ID"), env.get("PLACE_ID")
    if not universeId or not placeId:
        sys.exit("ERROR: project .env must contain UNIVERSE_ID and PLACE_ID")

    placePath = os.path.join(projectDir, "Place.rbxl")
    if not args.no_build:
        print("Running rojo build...")
        result = subprocess.run(
            "rojo build --output Place.rbxl",
            cwd=projectDir,
            capture_output=True,
            text=True,
            shell=True,
        )
        if result.returncode != 0:
            sys.exit(f"ERROR: rojo build failed:\n{result.stdout}\n{result.stderr}")
    if not os.path.isfile(placePath):
        sys.exit("ERROR: no Place.rbxl (rojo build did not produce one)")

    try:
        print("Uploading rojo build as Saved base version...")
        uploadResult = uploadPlace(placePath, universeId, placeId, "Saved")
        baseVersion = uploadResult["versionNumber"]
        print(f"Base version: {baseVersion}")
    finally:
        os.remove(placePath)

    config = buildConfig(projectDir)
    entryFiles = collectEntryFiles(projectDir, config)
    if not entryFiles:
        sys.exit("ERROR: no .sgfl entry files found — run save_v2 first (or sgfl migrate)")
    container = packContainer(entryFiles)
    print(f"Input container: {len(entryFiles)} files, {len(container)} bytes")

    scriptPath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_v2.luau")
    with open(scriptPath, "r", encoding="utf-8") as f:
        scriptSource = f.read()
    scriptSource = scriptSource.replace("__SGFL_CONFIG__", json.dumps(config))

    postapplyPath = os.path.join(projectDir, "postapply.luau")
    if os.path.isfile(postapplyPath):
        with open(postapplyPath, "r", encoding="utf-8") as f:
            hookSource = f.read()
        scriptSource = scriptSource.replace("__SGFL_POSTAPPLY__", "(function()\n" + hookSource + "\nend)()")
        print("postapply.luau hook included")
    else:
        scriptSource = scriptSource.replace("__SGFL_POSTAPPLY__", "nil")

    apiKey = getExecutionKey()
    headers = {"x-api-key": apiKey}
    binaryInputPath = createBinaryInput(headers, universeId, container)

    applyResult = runApplyTask(scriptSource, universeId, placeId, baseVersion, binaryInputPath, apiKey)
    print(f"Apply result: {json.dumps(applyResult, indent=2)}")
    if applyResult.get("failedRefs"):
        sys.exit("ERROR: apply left unresolved refs — aborting before upload")

    savedVersion = baseVersion + 1
    print(f"\nDownloading saved version {savedVersion} for sidecar patch-back...")
    savedBytes, _ = downloadPlace(placeId, savedVersion)

    patches = parseSidecarBlocks(projectDir, config)
    # props the session could read at save time but not write at apply time
    # (capability asymmetry) come back from the task for binary patching
    for item in applyResult.get("patchProps") or []:
        value = item["value"]
        if isinstance(value, str):
            value = value.encode("utf-8")
        patches[(item["className"], item["prop"])] = [value]
    if patches:
        patched, applied, problems = patch_file(savedBytes, patches)
        for problem in problems:
            print(f"  WARN(patch): {problem}")
        print(f"  patched {len(applied)}/{len(patches)} sidecar props")
        if len(applied) < len(patches):
            sys.exit("ERROR: some sidecar props could not be patched — aborting before upload")
        savedBytes = patched
    else:
        print("  no sidecar blocks found")

    versionType = "Published" if args.publish else "Saved"
    print(f"\nUploading final bytes as versionType={versionType}...")
    finalPath = os.path.join(projectDir, ".sgfl-publish.rbxl.tmp")
    with open(finalPath, "wb") as f:
        f.write(savedBytes)
    try:
        finalResult = uploadPlace(finalPath, universeId, placeId, versionType)
    finally:
        os.remove(finalPath)
    print(f"Done: version {finalResult['versionNumber']} ({versionType})")


if __name__ == "__main__":
    main()
