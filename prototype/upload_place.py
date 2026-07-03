"""Upload a place file via the Open Cloud v1 publish endpoint — the same
endpoint sgfl start/publish use. Defaults to versionType=Saved so the live
(published) place is not affected; pass --publish explicitly to publish.

Usage:
    py upload_place.py place.rbxl --env <project/.env>            # Saved
    py upload_place.py place.rbxl --env <project/.env> --publish  # Published!

The API key comes from SGFL_PUBLISH_KEY, or PUBLISH_KEY in ~/.sgfl/credentials
(loaded silently, never printed).
"""

import argparse
import os
import sys

import requests

REQUEST_TIMEOUT_SECONDS = 120


def loadDotEnv(path):
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def getPublishKey():
    key = os.environ.get("SGFL_PUBLISH_KEY")
    if key:
        return key
    credentialsPath = os.path.join(os.path.expanduser("~"), ".sgfl", "credentials")
    if os.path.isfile(credentialsPath):
        key = loadDotEnv(credentialsPath).get("PUBLISH_KEY")
        if key:
            return key
    sys.exit("ERROR: no publish key (SGFL_PUBLISH_KEY env var or PUBLISH_KEY in ~/.sgfl/credentials)")


def uploadPlace(filePath, universeId, placeId, versionType="Saved", apiKey=None):
    """Upload the file; returns the response JSON (contains versionNumber)."""
    apiKey = apiKey or getPublishKey()
    url = (
        f"https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions"
        f"?versionType={versionType}"
    )
    with open(filePath, "rb") as f:
        content = f.read()
    response = requests.post(
        url,
        headers={"x-api-key": apiKey, "Content-Type": "application/octet-stream"},
        data=content,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        sys.exit(f"ERROR: upload failed HTTP {response.status_code}: {response.text[:2000]}")
    return response.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="place file to upload")
    parser.add_argument("--env", help=".env file to read UNIVERSE_ID / PLACE_ID from")
    parser.add_argument("--universe", help="universe id (overrides --env)")
    parser.add_argument("--place", help="place id (overrides --env)")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="use versionType=Published (affects the LIVE place) instead of Saved",
    )
    args = parser.parse_args()

    universeId, placeId = args.universe, args.place
    if args.env:
        envValues = loadDotEnv(args.env)
        universeId = universeId or envValues.get("UNIVERSE_ID")
        placeId = placeId or envValues.get("PLACE_ID")
    if not universeId or not placeId:
        sys.exit("ERROR: need UNIVERSE_ID and PLACE_ID (via --env or --universe/--place)")

    versionType = "Published" if args.publish else "Saved"
    size = os.path.getsize(args.file)
    print(f"Uploading {args.file} ({size} bytes) to place {placeId} as versionType={versionType}...")
    result = uploadPlace(args.file, universeId, placeId, versionType)
    print(f"Uploaded: {result}")


if __name__ == "__main__":
    main()
