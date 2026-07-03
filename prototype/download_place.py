"""Download a place file (optionally a specific version) via the Roblox
asset-delivery API, using the same two-step flow as sgfl save: authenticated
metadata request -> unauthenticated signed `location` URL.

Usage:
    py download_place.py --env <project/.env> out.rbxl              # latest
    py download_place.py --env <project/.env> --version 55 out.rbxl

The API key comes from SGFL_DOWNLOAD_KEY, or DOWNLOAD_KEY in
~/.sgfl/credentials (loaded silently, never printed).
"""

import argparse
import os
import sys

import requests

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


def getDownloadKey():
    key = os.environ.get("SGFL_DOWNLOAD_KEY")
    if key:
        return key
    credentialsPath = os.path.join(os.path.expanduser("~"), ".sgfl", "credentials")
    if os.path.isfile(credentialsPath):
        key = loadDotEnv(credentialsPath).get("DOWNLOAD_KEY")
        if key:
            return key
    sys.exit("ERROR: no download key (SGFL_DOWNLOAD_KEY env var or DOWNLOAD_KEY in ~/.sgfl/credentials)")


def downloadPlace(placeId, version=None, apiKey=None):
    """Return (bytes, metadataDict) for the place, optionally a pinned version."""
    apiKey = apiKey or getDownloadKey()
    url = f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}"
    if version is not None:
        url += f"/version/{version}"
    response = requests.get(url, headers={"x-api-key": apiKey}, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        sys.exit(f"ERROR: asset-delivery request failed HTTP {response.status_code}: {response.text[:2000]}")
    payload = response.json()
    location = payload.get("location")
    if not location:
        sys.exit(f"ERROR: no location in asset-delivery response: {str(payload)[:2000]}")
    fileResponse = requests.get(location, timeout=DOWNLOAD_TIMEOUT_SECONDS)
    if fileResponse.status_code >= 400:
        sys.exit(f"ERROR: place download failed HTTP {fileResponse.status_code}")
    return fileResponse.content, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="path to write the downloaded place file to")
    parser.add_argument("--env", help=".env file to read PLACE_ID from")
    parser.add_argument("--place", help="place id (overrides --env)")
    parser.add_argument("--version", help="specific place version to download (default: latest)")
    args = parser.parse_args()

    placeId = args.place
    if args.env:
        placeId = placeId or loadDotEnv(args.env).get("PLACE_ID")
    if not placeId:
        sys.exit("ERROR: need PLACE_ID (via --env or --place)")

    content, payload = downloadPlace(placeId, args.version)
    with open(args.output, "wb") as f:
        f.write(content)
    versionNote = f" version {args.version}" if args.version else " (latest)"
    print(f"Downloaded place {placeId}{versionNote}: {args.output} ({len(content)} bytes)")
    for key in ("requestId", "isCopyrightProtected"):
        if key in payload:
            print(f"  {key}: {payload[key]}")


if __name__ == "__main__":
    main()
