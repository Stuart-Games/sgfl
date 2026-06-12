import glob
import getpass
import sys
import requests
import typer
from datetime import datetime
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


def _getAssetFolders() -> set[str]:
    configPath = (
        ASSET_CONFIG_FILE_PATH
        if os.path.exists(ASSET_CONFIG_FILE_PATH)
        else getAbsoluteFileURI("json/default.assets.json")
    )
    return {data["folder"] for data in getTableFromJsonFile(configPath).values()}


def _checkForOutdatedAssets():
    folders = _getAssetFolders()
    outdated = any(
        glob.glob(f"{folder}/*.rbxmx")
        for folder in folders
        if os.path.isdir(folder)
    )
    if outdated:
        raise SGFLError(
            "Project assets are in the outdated XML format (.rbxmx).",
            suggestions=["Run sgfl save to migrate assets to the binary format (.rbxm)."],
        )


def _deleteOutdatedAssets():
    folders = _getAssetFolders()
    for folder in folders:
        for path in glob.glob(f"{folder}/*.rbxmx"):
            os.remove(path)


def _runRojoBuild():
    runCommand(
        ["rojo", "build", "default.project.json", "-o", "Place.rbxl"],
        step="Compiling scripts via Rojo into Place.rbxl.",
        suggestions=[
            "Install Rojo or ensure it is available on PATH (rokit add rojo).",
            "Verify default.project.json exists and is valid.",
            "Run 'rojo build default.project.json -o Place.rbxl' manually to see detailed errors.",
        ],
    )


def startPlace(pull: bool):
    announceStep("Checking environment configuration for publish flow.")
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    userId = getEnvSafe("USER_ID")

    _checkForOutdatedAssets()

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

    _runRojoBuild()
    runLuauFile("lua/build.luau")

    # make correct publish req to roblox
    url = f"https://apis.roblox.com/universes/v1/{universeId}/places/{placeId}/versions?versionType=Published"
    headers = {"x-api-key": publishKey, "Content-Type": "application/octet-stream"}

    announceStep("Uploading built place file to Roblox.")
    try:
        with open(PLACE_FILE_PATH, "rb") as f:
            placeBinary = f.read()
    except OSError as exc:
        raise SGFLError(
            "Failed to read generated Place.rbxl file.",
            details=str(exc),
            suggestions=[
                "Check that rojo build and lua/build.luau completed successfully.",
                "Verify that Place.rbxl can be created in the project root.",
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
            ["code", "."],
            step="Opening project in VS Code.",
            captureOutput=False,
            shell=True,
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
            "Failed to write Place.rbxl to disk.",
            details=str(exc),
            suggestions=[
                "Check filesystem permissions in the project directory.",
                "Ensure there is enough disk space available.",
            ],
        )

    runLuauFile("lua/importAssets.luau")
    _deleteOutdatedAssets()

    deleteFile(PLACE_FILE_PATH)
    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} Saved place data to the local file system."
    )


def initPlace():
    announceStep("Creating SGFL project folder structure.")
    createDirIfNotExist("src")
    createDirIfNotExist("src/ReplicatedStorage")
    createDirIfNotExist("src/ReplicatedStorage/Util")
    createDirIfNotExist("src/ServerScriptService")
    createDirIfNotExist("src/StarterPlayerScripts")
    createDirIfNotExist("src/StarterCharacterScripts")
    createDirIfNotExist("src/ReplicatedFirst")

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


def _promptCredential(label: str, currentValue: Optional[str], hideInput: bool) -> Optional[str]:
    if currentValue:
        suffix = f" [{maskSecret(currentValue) if hideInput else currentValue}]"
        hint = " (press Enter to keep current value)"
    else:
        suffix = ""
        hint = ""
    prompt = f"{label}{suffix}{hint}: "
    if hideInput:
        entered = getpass.getpass(prompt)
    else:
        entered = input(prompt)
    entered = entered.strip()
    if entered == "":
        return None
    return entered


def authLogin(
    publishKey: Optional[str] = None,
    downloadKey: Optional[str] = None,
    userId: Optional[str] = None,
):
    interactive = (
        publishKey is None and downloadKey is None and userId is None
    )

    if interactive:
        if not sys.stdin.isatty():
            raise SGFLError(
                "Refusing to prompt for credentials in a non-interactive session.",
                details="sgfl auth login requires a TTY when called without flags.",
                suggestions=[
                    "Run sgfl auth login from an interactive terminal.",
                    "For scripts, pass --publish-key / --download-key / --user-id directly.",
                ],
            )

        loadCredentials()
        current = {key: os.environ.get(key) for key in CREDENTIAL_KEYS}

        announceStep(f"Updating credentials at {CREDENTIALS_PATH}.")
        print("Press Enter to keep an existing value; type a new value to replace it.\n")

        publishKey = _promptCredential("PUBLISH_KEY", current["PUBLISH_KEY"], hideInput=True)
        downloadKey = _promptCredential("DOWNLOAD_KEY", current["DOWNLOAD_KEY"], hideInput=True)
        userId = _promptCredential("USER_ID", current["USER_ID"], hideInput=False)

        if publishKey is None and downloadKey is None and userId is None:
            print(f"\n{color.YELLOW}{color.BOLD}NOOP{color.END} No credentials changed.")
            return

    values = {
        "PUBLISH_KEY": publishKey,
        "DOWNLOAD_KEY": downloadKey,
        "USER_ID": userId,
    }

    if userId is not None and not userId.isdigit():
        raise SGFLError(
            "USER_ID must be numeric.",
            details=f"Got: {userId}",
            suggestions=[
                "USER_ID is your numeric Roblox user ID (no username, no hyphens).",
            ],
        )

    path = saveCredentials(values)
    updated = [key for key, val in values.items() if val is not None]
    print(
        f"\n{color.GREEN}{color.BOLD}SUCCESS{color.END} "
        f"Wrote credentials to {path} (updated: {', '.join(updated)})."
    )


def runUpdate():
    announceStep("Upgrading sgfl via pipx.")

    runCommand(
        ["pipx", "upgrade", "sgfl"],
        suggestions=[
            "Ensure pipx is installed and on PATH (see https://pipx.pypa.io/).",
            "If sgfl was not installed via pipx, reinstall with: pipx install --force git+https://github.com/devvf/sgfl.git",
        ],
        captureOutput=False,
    )

    invalidateUpdateCache()

    print(
        f"\n{color.GREEN}{color.BOLD}SUCCESS{color.END} "
        f"sgfl upgraded. Re-run your previous command to use the new version."
    )


def authStatus():
    announceStep(f"Inspecting credentials at {CREDENTIALS_PATH}.")
    if not os.path.exists(CREDENTIALS_PATH):
        print(
            f"{color.YELLOW}{color.BOLD}MISSING{color.END} "
            f"No credentials file at {CREDENTIALS_PATH}.\n"
            f"Run {color.BOLD}sgfl auth login{color.END} to create one."
        )
        return

    loadCredentials()

    print(f"File: {CREDENTIALS_PATH}")
    try:
        mode = oct(os.stat(CREDENTIALS_PATH).st_mode & 0o777)
        print(f"Mode: {mode}")
    except OSError:
        pass

    for key in CREDENTIAL_KEYS:
        value = os.environ.get(key)
        if value is None or value.strip() == "":
            print(f"  - {key}: {color.RED}MISSING{color.END}")
            continue

        trimmed = value.strip()
        notes: list[str] = []
        if value != trimmed:
            notes.append("contains leading/trailing whitespace")
        if key == "USER_ID" and not trimmed.isdigit():
            notes.append("should be numeric")
        if key in ("PUBLISH_KEY", "DOWNLOAD_KEY") and len(trimmed) < 16:
            notes.append("looks unusually short for an API key")

        display = (
            maskSecret(trimmed)
            if key in ("PUBLISH_KEY", "DOWNLOAD_KEY")
            else trimmed
        )
        line = f"  - {key}: {color.GREEN}SET{color.END} value={display} (len={len(trimmed)})"
        if notes:
            line += "; notes: " + "; ".join(notes)
        print(line)


def _formatPlaceFileSummary(path: str) -> str:
    sizeBytes = os.path.getsize(path)
    sizeMb = sizeBytes / (1024 * 1024)
    mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")
    return f"Place.rbxl: {sizeMb:.2f} MB, modified {mtime}"


def _publishPlaceFile(
    *,
    name: str,
    placeId: str,
    universeId: str,
    publishKey: str,
    placeBinary: bytes,
    versionType: str,
) -> Optional[dict]:
    url = (
        f"https://apis.roblox.com/universes/v1/{universeId}"
        f"/places/{placeId}/versions?versionType={versionType}"
    )
    headers = {"x-api-key": publishKey, "Content-Type": "application/octet-stream"}

    announceStep(f"Uploading to {name} ({placeId})...")
    try:
        res = requests.post(
            url, headers=headers, data=placeBinary, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        return {
            "name": name,
            "placeId": placeId,
            "reason": f"network error: {exc}",
            "diagnostics": _httpDiagnostics(
                method="POST",
                url=url,
                apiKeyName="PUBLISH_KEY",
                apiKey=publishKey,
                error=exc,
            ),
        }

    if res.status_code != 200:
        return {
            "name": name,
            "placeId": placeId,
            "reason": f"HTTP {res.status_code}: {_responseReason(res)}",
            "statusCode": res.status_code,
            "diagnostics": _httpDiagnostics(
                method="POST",
                url=url,
                apiKeyName="PUBLISH_KEY",
                apiKey=publishKey,
                response=res,
            ),
        }

    print(f"{color.GREEN}{color.BOLD}OK{color.END}   {name} published successfully.")
    return None


def publishPlaces(
    env: str,
    *,
    dryRun: bool = False,
    noBuild: bool = False,
    placesFilter: Optional[list[str]] = None,
    versionType: str = "Published",
):
    announceStep(f"Loading environment file .env.{env}.")
    loadEnvFile(env)

    announceStep("Reading universe configuration.")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    places = discoverPlaceIds()

    if placesFilter:
        normalized = [p.strip().lower() for p in placesFilter if p.strip()]
        unknown = [p for p in normalized if p not in places]
        if unknown:
            raise SGFLError(
                "Unknown place name in --places filter.",
                details=(
                    f"Filter referenced: {', '.join(unknown)}. "
                    f"Available: {', '.join(sorted(places.keys()))}."
                ),
                suggestions=[
                    "Use names defined as PLACE_ID_<NAME> in your env file (case-insensitive).",
                    "Drop --places to publish to every declared place.",
                ],
            )
        places = {name: places[name] for name in normalized}

    _checkForOutdatedAssets()

    if noBuild and not os.path.exists(PLACE_FILE_PATH):
        raise SGFLError(
            "--no-build was passed but Place.rbxl does not exist.",
            details=f"Expected file at: {PLACE_FILE_PATH}.",
            suggestions=[
                "Run sgfl publish without --no-build to build first.",
                "Run lua/build.luau manually if you have a custom build pipeline.",
            ],
        )

    summaryLines = [
        f"{color.CYAN}{color.BOLD}INFO{color.END} About to publish to universe {universeId} (env={env}):"
    ]
    nameWidth = max(len(name) for name in places.keys())
    for name in sorted(places.keys()):
        summaryLines.append(f"        - {name.ljust(nameWidth)}  ->  {places[name]}")
    summaryLines.append(f"{color.CYAN}{color.BOLD}INFO{color.END} versionType: {versionType}")
    if noBuild:
        summaryLines.append(
            f"{color.CYAN}{color.BOLD}INFO{color.END} {_formatPlaceFileSummary(PLACE_FILE_PATH)} (existing — --no-build)"
        )
    else:
        summaryLines.append(
            f"{color.CYAN}{color.BOLD}INFO{color.END} Place.rbxl will be built fresh via rojo + lua/build.luau."
        )
    if dryRun:
        summaryLines.append(f"{color.YELLOW}{color.BOLD}DRY-RUN{color.END} no upload will be performed.")

    if dryRun:
        print()
        for line in summaryLines:
            print(line)
        print(
            f"\n{color.YELLOW}{color.BOLD}DRY-RUN{color.END} would publish to {len(places)} place(s); skipping confirmation, build, and upload."
        )
        return

    confirmPublish(env, summaryLines)

    toggleMessage = (
        f"{color.RED}{color.BOLD}PLEASE MOVE TO YES TO CONFIRM{color.END}"
        f"  (←/→ to choose, Enter to commit)"
    )
    if not confirmToggle(toggleMessage, defaultYes=False):
        announceStep("Aborted by user.")
        raise typer.Exit(code=0)

    if not noBuild:
        _runRojoBuild()
        runLuauFile("lua/build.luau")

    try:
        with open(PLACE_FILE_PATH, "rb") as f:
            placeBinary = f.read()
    except OSError as exc:
        raise SGFLError(
            "Failed to read generated Place.rbxl file.",
            details=str(exc),
            suggestions=[
                "Check that rojo build and lua/build.luau completed successfully.",
                "Re-run without --no-build to regenerate Place.rbxl.",
            ],
        )

    failures: list[dict] = []
    for name in sorted(places.keys()):
        failure = _publishPlaceFile(
            name=name,
            placeId=places[name],
            universeId=universeId,
            publishKey=publishKey,
            placeBinary=placeBinary,
            versionType=versionType,
        )
        if failure is not None:
            failures.append(failure)

    print(f"\n{color.BOLD}Publish summary{color.END} (env={env}, universe={universeId}):")
    nameWidth = max(len(name) for name in places.keys())
    failedNames = {f["name"] for f in failures}
    for name in sorted(places.keys()):
        marker = (
            f"{color.RED}{color.BOLD}FAIL{color.END}"
            if name in failedNames
            else f"{color.GREEN}{color.BOLD} OK {color.END}"
        )
        line = f"  {marker}  {name.ljust(nameWidth)}  ->  {places[name]}"
        if name in failedNames:
            reason = next(f["reason"] for f in failures if f["name"] == name)
            line += f"   {reason}"
        print(line)

    deleteFile(PLACE_FILE_PATH)

    if failures:
        anyAuthError = any(
            f.get("statusCode") in (401, 403) for f in failures
        )
        suggestions = [
            "Inspect each failed place's HTTP diagnostics with --detailed.",
            "Confirm UNIVERSE_ID and every PLACE_ID_<NAME> point to the right place.",
        ]
        if anyAuthError:
            suggestions.append(
                "PUBLISH_KEY may lack permissions on one or more places — check scopes and allowed-IP settings in the Creator Dashboard."
            )

        diagnostics: dict[str, list[str]] = {}
        for f in failures:
            diagnostics[f"HTTP diagnostics ({f['name']})"] = f["diagnostics"]

        raise SGFLError(
            f"{len(failures)} of {len(places)} place(s) failed to publish.",
            details="; ".join(f"{f['name']}: {f['reason']}" for f in failures),
            suggestions=suggestions,
            diagnostics=diagnostics,
        )

    print(
        f"\n{color.GREEN}{color.BOLD}SUCCESS{color.END} Published to {len(places)} place(s) in universe {universeId}."
    )
