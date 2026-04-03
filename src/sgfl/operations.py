import requests
from urllib.parse import urlparse
from typing import Optional
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


def _clipText(text: str, maxChars: int = 600) -> str:
    trimmed = text.strip()
    if len(trimmed) <= maxChars:
        return trimmed
    return trimmed[:maxChars] + "\n...[response truncated]"


def _httpDiagnostics(
    *,
    method: str,
    url: str,
    apiKeyName: str,
    apiKey: str,
    response: Optional[requests.Response] = None,
    error: Optional[Exception] = None,
) -> list[str]:
    parsed = urlparse(url)
    lines = [
        f"request: {method} {url}",
        f"endpoint host: {parsed.netloc}",
        f"endpoint path: {parsed.path}",
        f"auth: x-api-key set via {apiKeyName} (masked={maskSecret(apiKey)}, len={len(apiKey.strip())})",
    ]

    if error is not None:
        lines.append(f"request error type: {type(error).__name__}")
        lines.append(f"request error text: {error}")

    if response is not None:
        lines.append(f"status: {response.status_code}")
        lines.append(f"reason: {response.reason or 'unknown'}")

        contentType = response.headers.get("Content-Type", "missing")
        lines.append(f"content-type: {contentType}")

        try:
            elapsedMs = int(response.elapsed.total_seconds() * 1000)
            lines.append(f"latency: {elapsedMs}ms")
        except Exception:
            pass

        interestingHeaderKeys = [
            "x-request-id",
            "x-correlation-id",
            "x-trace-id",
            "roblox-machine-id",
            "roblox-id",
            "x-roblox-region",
        ]
        for headerKey in interestingHeaderKeys:
            if headerKey in response.headers:
                lines.append(
                    f"response header {headerKey}: {response.headers.get(headerKey)}"
                )

        try:
            payload = response.json()
            if isinstance(payload, dict):
                lines.append(f"json keys: {', '.join(payload.keys())}")
        except ValueError:
            bodyPreview = _clipText(response.text)
            if bodyPreview:
                lines.append("response body preview:")
                lines.extend([f"  {line}" for line in bodyPreview.splitlines()])

    return lines


def _withAuthorizationWarning(
    baseSuggestions: list[str],
    statusCode: int,
    usesApiKey: bool = True,
) -> list[str]:
    if statusCode not in [401, 403]:
        return baseSuggestions

    warning = (
        "Authorization issue detected (HTTP 401/403). "
        "Your Roblox API key may have auto-expired; check your key status, scopes, and allowed IP settings in the Creator Dashboard. "
        "Review the Roblox API key reference for required setup."
    )

    if not usesApiKey:
        warning = (
            "Authorization issue detected (HTTP 401/403). "
            "The download URL may have expired; request a fresh URL and retry. "
            "If this persists, also verify Roblox API key setup and scopes in the Creator Dashboard."
        )

    return [*baseSuggestions, warning]


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
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="POST",
                    url=url,
                    apiKeyName="PUBLISH_KEY",
                    apiKey=publishKey,
                    error=exc,
                )
            },
        )

    if res.status_code != 200:
        suggestions = _withAuthorizationWarning(
            [
                "Confirm PLACE_ID and UNIVERSE_ID point to the correct place.",
                "Regenerate your PUBLISH_KEY and ensure it has publish permissions.",
                "Check that your account can edit the target place.",
            ],
            res.status_code,
            usesApiKey=True,
        )
        raise SGFLError(
            "Roblox rejected the publish request.",
            details=f"HTTP {res.status_code}: {_responseReason(res)}",
            suggestions=suggestions,
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="POST",
                    url=url,
                    apiKeyName="PUBLISH_KEY",
                    apiKey=publishKey,
                    response=res,
                )
            },
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
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="GET",
                    url=url,
                    apiKeyName="DOWNLOAD_KEY",
                    apiKey=downloadKey,
                    error=exc,
                )
            },
        )

    if res.status_code != 200:
        suggestions = _withAuthorizationWarning(
            [
                "Check DOWNLOAD_KEY permissions for asset delivery.",
                "Ensure PLACE_ID refers to an accessible place.",
            ],
            res.status_code,
            usesApiKey=True,
        )
        raise SGFLError(
            "Roblox did not return a download URL.",
            details=f"HTTP {res.status_code}: {_responseReason(res)}",
            suggestions=suggestions,
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="GET",
                    url=url,
                    apiKeyName="DOWNLOAD_KEY",
                    apiKey=downloadKey,
                    response=res,
                )
            },
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
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="GET",
                    url=url,
                    apiKeyName="DOWNLOAD_KEY",
                    apiKey=downloadKey,
                    response=res,
                )
            },
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
            diagnostics={
                "HTTP diagnostics": _httpDiagnostics(
                    method="GET",
                    url=url,
                    apiKeyName="DOWNLOAD_KEY",
                    apiKey=downloadKey,
                    response=res,
                )
            },
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
            diagnostics={
                "HTTP diagnostics": [
                    f"request: GET {downloadUrl}",
                    f"request error type: {type(exc).__name__}",
                    f"request error text: {exc}",
                ]
            },
        )

    if downloadRes.status_code != 200:
        suggestions = _withAuthorizationWarning(
            [
                "Retry download, then inspect response details for permission or availability issues."
            ],
            downloadRes.status_code,
            usesApiKey=False,
        )
        raise SGFLError(
            "Place file download failed.",
            details=f"HTTP {downloadRes.status_code}: {_responseReason(downloadRes)}",
            suggestions=suggestions,
            diagnostics={
                "HTTP diagnostics": [
                    f"request: GET {downloadUrl}",
                    f"status: {downloadRes.status_code}",
                    f"content-type: {downloadRes.headers.get('Content-Type', 'missing')}",
                ]
            },
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
