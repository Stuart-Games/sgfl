"""Driver for Open Cloud Luau Execution Session prototyping.

Creates a Luau execution task on a place, polls it to completion, then prints
the task's logs and JSON results.

Usage:
    py run_task.py projection_probe.luau --env ../../VesselBuilder/.env
    py run_task.py projection_probe.luau --universe 123 --place 456

The API key is read from the SGFL_EXECUTION_KEY environment variable. It needs
the "Luau Execution Sessions" API system with read + write scopes for the
target universe (Creator Hub -> Open Cloud -> API Keys).
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


def loadDotEnv(path):
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", help="path to the .luau file to execute")
    parser.add_argument("--env", help=".env file to read UNIVERSE_ID / PLACE_ID from")
    parser.add_argument("--universe", help="universe id (overrides --env)")
    parser.add_argument("--place", help="place id (overrides --env)")
    parser.add_argument("--timeout", default="120s", help="task timeout, e.g. 60s (max 300s)")
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
    createUrl = f"{BASE}/universes/{universeId}/places/{placeId}/luau-execution-session-tasks"
    print(f"Creating task on universe {universeId}, place {placeId} ({len(scriptSource)} byte script)...")
    response = requests.post(
        createUrl,
        headers=headers,
        json={"script": scriptSource, "timeout": args.timeout},
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

    print("\n=== RESULTS ===")
    print(json.dumps(task.get("output", {}).get("results", []), indent=2))


if __name__ == "__main__":
    main()
