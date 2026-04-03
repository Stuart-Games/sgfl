import requests
from sys import platform
from .util import *

REQUEST_TIMEOUT_SECONDS = 30


def _responseReason(res: requests.Response) -> str:
    contentType = res.headers.get("Content-Type", "")

    if "application/json" in contentType:
        try:
            payload = res.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            for key in ["message", "error", "errors"]:
                if key in payload:
                    return f"{payload[key]}"

    text = res.text.strip()
    if text:
        return text[:500]

    return "No additional response body was returned."


def startPlace(pull: bool):
    announceStep("Checking environment configuration for publish flow.")
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    userId = getEnvSafe("USER_ID")

    # pull and build
    if pull:
        runCommand(
            ["git", "pull"],
            step="Pulling latest repository changes before build.",
            suggestions=[
                "Resolve any local git conflicts, then re-run sgfl start.",
                "Run git pull manually to inspect repository errors.",
            ],
            captureOutput=False,
        )

    runLuauFile("lua/build.luau")

    # make correct publish req to roblox
    url = f"https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published"
    headers = {"x-api-key": publishKey, "Content-Type": "application/xml"}

    announceStep("Uploading built place file to Roblox.")
    try:
        with open(PLACE_FILE_PATH, "rb") as f:
            placeBinary = f.read()
    except OSError as exc:
        raise SGFLError(
            "Failed to read generated Place.rbxlx file.",
            details=str(exc),
            suggestions=[
                "Check that lua/build.luau completed successfully.",
                "Verify that Place.rbxlx can be created in the project root.",
            ],
        )

    try:
        res = requests.post(
            url, headers=headers, data=placeBinary, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise SGFLError(
            "Publish request to Roblox failed.",
            details=str(exc),
            suggestions=[
                "Verify internet connectivity and try again.",
                "Confirm your PLACE_ID, UNIVERSE_ID and PUBLISH_KEY values are valid.",
            ],
        )

    if res.status_code != 200:
        raise SGFLError(
            "Roblox rejected the publish request.",
            details=f"HTTP {res.status_code}: {_responseReason(res)}",
            suggestions=[
                "Confirm PLACE_ID and UNIVERSE_ID point to the correct place.",
                "Regenerate your PUBLISH_KEY and ensure it has publish permissions.",
                "Check that your account can edit the target place.",
            ],
        )
    else:
        placeOpenString = f"roblox-studio:1+userId:{userId}+task:EditPlace+placeId:{placeId}+universeId:{universeId}"

        # open studio (generic window)
        if platform == "win32":  # windows (any ver)
            runCommand(
                ["cmd", "/c", "start", "", placeOpenString],
                step="Opening Roblox Studio for the published place.",
                captureOutput=False,
            )
        elif platform == "darwin":  # macos
            runCommand(
                ["open", placeOpenString],
                step="Opening Roblox Studio for the published place.",
                captureOutput=False,
            )

        deleteFile(PLACE_FILE_PATH)
        runCommand(
            ["code", "."], step="Opening project in VS Code.", captureOutput=False
        )
        announceStep("Starting Rojo server.")
        runCommand(
            ["rojo", "serve"],
            suggestions=[
                "Install Rojo or ensure it is available on PATH.",
                "Run rojo serve manually to inspect detailed setup issues.",
            ],
            captureOutput=False,
        )


def savePlace():
    announceStep("Checking environment configuration for save flow.")
    placeId = getEnvSafe("PLACE_ID")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")

    # make correct publish req to roblox
    url = f"https://apis.roblox.com/asset-delivery-api/v1/assetId/{placeId}"
    headers = {"x-api-key": downloadKey}

    announceStep("Requesting secure download URL from Roblox.")
    try:
        res = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise SGFLError(
            "Failed to request place download URL.",
            details=str(exc),
            suggestions=[
                "Verify internet connectivity and try again.",
                "Confirm DOWNLOAD_KEY and PLACE_ID are valid.",
            ],
        )

    if res.status_code != 200:
        raise SGFLError(
            "Roblox did not return a download URL.",
            details=f"HTTP {res.status_code}: {_responseReason(res)}",
            suggestions=[
                "Check DOWNLOAD_KEY permissions for asset delivery.",
                "Ensure PLACE_ID refers to an accessible place.",
            ],
        )

    try:
        payload = res.json()
    except ValueError:
        raise SGFLError(
            "Roblox returned an invalid JSON payload.",
            details="Expected a JSON response containing 'location'.",
            suggestions=[
                "Retry the request in case of a temporary Roblox API issue.",
                "Inspect API response manually to confirm the endpoint output.",
            ],
        )

    downloadUrl = payload.get("location")

    if not downloadUrl:
        raise SGFLError(
            "Roblox response did not include a download location.",
            details=f"Response payload keys: {', '.join(payload.keys()) if isinstance(payload, dict) else 'non-dict payload'}",
            suggestions=[
                "Verify DOWNLOAD_KEY has the right scope.",
                "Confirm the place has a published version available.",
            ],
        )

    announceStep("Downloading place file data.")
    try:
        downloadRes = requests.get(downloadUrl, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise SGFLError(
            "Failed to download place data from Roblox.",
            details=str(exc),
            suggestions=[
                "Retry the save command.",
                "Check your network connection and VPN/proxy settings.",
            ],
        )

    if downloadRes.status_code != 200:
        raise SGFLError(
            "Place file download failed.",
            details=f"HTTP {downloadRes.status_code}: {_responseReason(downloadRes)}",
            suggestions=[
                "Retry download, then inspect response details for permission or availability issues."
            ],
        )

    placeData = downloadRes.content

    # write to place file
    announceStep("Writing downloaded place file to disk.")
    try:
        with open(PLACE_FILE_PATH, "wb") as f:
            f.write(placeData)
    except OSError as exc:
        raise SGFLError(
            "Failed to write Place.rbxlx to disk.",
            details=str(exc),
            suggestions=[
                "Check filesystem permissions in the project directory.",
                "Ensure there is enough disk space available.",
            ],
        )

    runLuauFile("lua/importAssets.luau")

    deleteFile(PLACE_FILE_PATH)
    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} Saved place data to the local file system."
    )


def initPlace():
    announceStep("Creating SGFL project folder structure.")
    createDirIfNotExist("src")
    createDirIfNotExist("src/client")
    createDirIfNotExist("src/server")
    createDirIfNotExist("src/shared")
    createDirIfNotExist("src/shared/Util")

    open("src/client/init.client.luau", "w")
    open("src/server/init.server.luau", "w")

    assetTable = getTableFromJsonFile("json/default.assets.json")

    for _, data in assetTable.items():
        createDirIfNotExist(data["folder"])

    # https://stackoverflow.com/a/12309296
    with open("assets.json", "w", encoding="utf-8") as f:
        json.dump(assetTable, f, ensure_ascii=False, indent=4)

    with open("default.project.json", "w", encoding="utf-8") as f:
        fileJson = getTableFromJsonFile("json/default.project.json")
        json.dump(fileJson, f, ensure_ascii=False, indent=4)

    with open("README.md", "w") as f:
        f.write("Generated by SGFL!\n")

    with open(".env", "w") as f:
        f.write("PLACE_ID=\n")
        f.write("UNIVERSE_ID=\n")
        f.write("PUBLISH_KEY=\n")
        f.write("DOWNLOAD_KEY=\n")
        f.write("USER_ID=")

    with open(getAbsoluteFileURI("misc/gitignore.txt"), "r") as f:
        text = f.readlines()
        with open(".gitignore", "w") as g:
            g.writelines(text)

    runCommand(["rokit", "init"], step="Initializing Rokit toolchain.")
    runCommand(["rokit", "add", "lune"], step="Adding Lune to toolchain.")
    runCommand(["rokit", "add", "rojo"], step="Adding Rojo to toolchain.")
    runCommand(["rokit", "install"], step="Installing toolchain dependencies.")

    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} Created new sgfl instance. To get started run {color.BOLD}sgfl start{color.END}."
    )
