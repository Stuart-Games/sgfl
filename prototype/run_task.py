"""Driver for Open Cloud Luau Execution Session prototyping.

Creates a Luau execution task on a place (optionally pinned to a specific
place version), polls it to completion, then prints the task's logs and JSON
results. Supports binary input (file made available to the script as a buffer)
and binary output (script returns a buffer downloaded to a local file).

Usage:
    py run_task.py projection_probe.luau --env ../../VesselBuilder/.env
    py run_task.py probe.luau --universe 123 --place 456
    py run_task.py probe.luau --env .env --version 54
    py run_task.py probe.luau --env .env --binary-output out.bin
    py run_task.py probe.luau --env .env --binary-input entries.bin

With --binary-output, the script must return a single table:
    return { BinaryOutput = someBuffer, ReturnValues = { ... } }
With --binary-input, the script reads the file via its first vararg:
    local taskInput = ({...})[1]
    local buf: buffer = taskInput.BinaryInput

The API key is read from the SGFL_EXECUTION_KEY environment variable, falling
back to ~/.sgfl/execution.key. It needs the "Luau Execution Sessions" API
system with read + write scopes for the target universe (Creator Hub ->
Open Cloud -> API Keys).

NOTE: task creation is rate-limited to 5 calls/minute per API key owner.
"""

import argparse
import json
import os
import sys
import time

import requests

BASE = "https://apis.roblox.com/cloud/v2"
POLL_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 300


def loadDotEnv(path):
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def uploadBinaryInput(headers, universeId, filePath):
    """Create a LuauExecutionSessionTaskBinaryInput, upload the file to its
    presigned URI, and return the resource path to reference at task creation."""
    size = os.path.getsize(filePath)
    print(f"Creating binary input ({size} bytes)...")
    response = requests.post(
        f"{BASE}/universes/{universeId}/luau-execution-session-task-binary-inputs",
        headers=headers,
        json={"size": size},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        sys.exit(f"ERROR: create binary input failed HTTP {response.status_code}: {response.text[:2000]}")
    binaryInput = response.json()
    with open(filePath, "rb") as f:
        uploadResponse = requests.put(
            binaryInput["uploadUri"],
            data=f,
            headers={"Content-Type": "application/octet-stream"},
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
    if uploadResponse.status_code >= 400:
        sys.exit(
            f"ERROR: binary input upload failed HTTP {uploadResponse.status_code}: {uploadResponse.text[:2000]}"
        )
    print(f"Binary input uploaded: {binaryInput['path']}")
    return binaryInput["path"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", help="path to the .luau file to execute")
    parser.add_argument("--env", help=".env file to read UNIVERSE_ID / PLACE_ID from")
    parser.add_argument("--universe", help="universe id (overrides --env)")
    parser.add_argument("--place", help="place id (overrides --env)")
    parser.add_argument("--version", help="place version id to pin the task to (e.g. a Saved version)")
    parser.add_argument("--timeout", default="120s", help="task timeout, e.g. 60s (max 300s)")
    parser.add_argument("--binary-input", help="file to upload and expose to the script as taskInput.BinaryInput")
    parser.add_argument("--binary-output", help="file to save the script's BinaryOutput buffer to")
    parser.add_argument("--out", help="file to save the JSON results to (in addition to printing)")
    args = parser.parse_args()

    universeId, placeId = args.universe, args.place
    if args.env:
        envValues = loadDotEnv(args.env)
        universeId = universeId or envValues.get("UNIVERSE_ID")
        placeId = placeId or envValues.get("PLACE_ID")
    if not universeId or not placeId:
        sys.exit("ERROR: need UNIVERSE_ID and PLACE_ID (via --env or --universe/--place)")

    apiKey = os.environ.get("SGFL_EXECUTION_KEY")
    if not apiKey:
        keyPath = os.path.join(os.path.expanduser("~"), ".sgfl", "execution.key")
        if os.path.isfile(keyPath):
            with open(keyPath, "r", encoding="utf-8") as f:
                apiKey = f.read().strip()
    if not apiKey:
        sys.exit(
            "ERROR: no API key. Set SGFL_EXECUTION_KEY or write the key to ~/.sgfl/execution.key"
        )

    with open(args.script, "r", encoding="utf-8") as f:
        scriptSource = f.read()

    headers = {"x-api-key": apiKey}

    body = {"script": scriptSource, "timeout": args.timeout}
    if args.binary_input:
        body["binaryInput"] = uploadBinaryInput(headers, universeId, args.binary_input)
    if args.binary_output:
        body["enableBinaryOutput"] = True

    placePath = f"universes/{universeId}/places/{placeId}"
    if args.version:
        placePath += f"/versions/{args.version}"
    createUrl = f"{BASE}/{placePath}/luau-execution-session-tasks"
    versionNote = f", version {args.version}" if args.version else ""
    print(
        f"Creating task on universe {universeId}, place {placeId}{versionNote} "
        f"({len(scriptSource)} byte script)..."
    )
    response = requests.post(createUrl, headers=headers, json=body, timeout=REQUEST_TIMEOUT_SECONDS)
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

    logsResponse = requests.get(
        f"{BASE}/{taskPath}/logs", headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
    )
    if logsResponse.ok:
        for chunk in logsResponse.json().get("luauExecutionSessionTaskLogs", []):
            for message in chunk.get("messages", []):
                print(f"  [log] {message}")

    if task["state"] != "COMPLETE":
        print(f"\nTask ended in state {task['state']}")
        print(json.dumps(task.get("error", {}), indent=2))
        sys.exit(1)

    if args.binary_output:
        binaryOutputUri = task.get("binaryOutputUri")
        if binaryOutputUri:
            print("Downloading binary output...")
            downloadResponse = requests.get(binaryOutputUri, timeout=DOWNLOAD_TIMEOUT_SECONDS)
            if downloadResponse.status_code >= 400:
                sys.exit(
                    f"ERROR: binary output download failed HTTP {downloadResponse.status_code}: "
                    f"{downloadResponse.text[:2000]}"
                )
            with open(args.binary_output, "wb") as f:
                f.write(downloadResponse.content)
            print(f"Binary output saved: {args.binary_output} ({len(downloadResponse.content)} bytes)")
        else:
            print("WARN: --binary-output requested but task returned no binaryOutputUri")

    results = task.get("output", {}).get("results", [])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"Results saved: {args.out}")

    print("\n=== RESULTS ===")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
