import getpass
import hashlib
import shutil
import socket
import sys
import requests
import typer
from datetime import datetime
from urllib.parse import urlparse
from typing import Optional
from sys import platform
from .util import *
from .util import _getInstalledVersion  # private: not covered by the star import
from . import cloud

REQUEST_TIMEOUT_SECONDS = 30
# Place uploads are multi-megabyte bodies; the 30s API timeout is not enough
# for them on a slow uplink.
UPLOAD_TIMEOUT_SECONDS = 300


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


def _loadAssetTable() -> dict:
    """Read and normalize this project's assets.json.

    Never falls back to the packaged default table: that default describes a
    different project, so silently adopting it (the usual cause is running sgfl
    from the wrong directory, or a subdirectory of the project) would have save
    write entry files into invented folders and publish apply the wrong set of
    entries. `sgfl init` is the only command that touches the default."""
    # resolved per call rather than via the import-time ASSET_CONFIG_FILE_PATH
    # constant, so the path always reflects the actual working directory
    configPath = getFileURI("assets.json")
    if not os.path.exists(configPath):
        raise SGFLError(
            "No assets.json found in the current directory.",
            details=f"Looked for: {configPath}.",
            suggestions=[
                "Run sgfl from the project root (the folder holding assets.json and default.project.json).",
                "Run sgfl init to scaffold a new project here.",
                "sgfl no longer falls back to its built-in default entry list — if you relied on that, "
                "commit an assets.json (sgfl init writes the default one).",
            ],
        )
    return cloud.normalizeAssetConfig(getTableFromJsonFile(configPath))


def _legacyEntryFiles(assetTable: dict) -> list[str]:
    """Entry-named legacy files from the Lune pipeline ({folder}/{Entry}.rbxm
    or .rbxmx). Exact names only — new-format blobs end in .sgfl.rbxm and
    other files in shared asset folders are never touched."""
    legacy: list[str] = []
    for name, spec in assetTable.items():
        for suffix in (".rbxm", ".rbxmx"):
            path = f"{spec['folder']}/{name}{suffix}"
            if os.path.isfile(path):
                legacy.append(path)
    return legacy


def _checkForLegacyAssets(assetTable: dict):
    legacy = _legacyEntryFiles(assetTable)
    if legacy:
        raise SGFLError(
            "This project still has legacy .rbxm asset files from the old pipeline.",
            details=f"Found {len(legacy)} legacy entry file(s), e.g. {legacy[0]}.",
            suggestions=[
                "Run sgfl migrate to convert the project to the new format.",
                "Older sgfl versions keep working on unmigrated projects if you are not ready.",
            ],
        )


# Wally dependency sync (v2.10.0). `default.project.json` mounts Packages/, so
# a rojo build on a fresh clone (or after a wally.toml change someone else
# pushed) is silently wrong until `wally install` runs. Following the
# Cargo/Go model, dependency installation is simply a phase of every build —
# no prompt: `wally install` is deterministic given wally.lock, local, and
# reversible, so there is no decision for a human to make. Projects without a
# wally.toml are untouched.
WALLY_MANIFEST = "wally.toml"
WALLY_LOCKFILE = "wally.lock"
# Every directory wally can install into (shared / server / dev realms).
WALLY_PACKAGE_DIRS = ("Packages", "ServerPackages", "DevPackages")
WALLY_STAMP_NAME = ".sgfl-wally-stamp"


def _wallyStamp() -> str:
    digest = hashlib.sha256()
    for name in (WALLY_MANIFEST, WALLY_LOCKFILE):
        try:
            with open(name, "rb") as f:
                digest.update(f.read())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\x00")
    return digest.hexdigest()


def _wallyPackagesFresh() -> bool:
    """True iff the installed packages were produced from the current
    wally.toml + wally.lock. `wally install` is not incremental — it rebuilds
    the package directories from scratch every run — so this stamp check is
    what keeps the already-up-to-date case at zero subprocesses."""
    expected = _wallyStamp()
    matched = False
    for dirName in WALLY_PACKAGE_DIRS:
        if not os.path.isdir(dirName):
            continue
        try:
            with open(os.path.join(dirName, WALLY_STAMP_NAME), "r", encoding="utf-8") as f:
                stamp = f.read().strip()
        except OSError:
            return False
        if stamp != expected:
            return False
        matched = True
    return matched


def _writeWallyStamps():
    # Recompute rather than reuse: `wally install` may have rewritten
    # wally.lock (first install, or a manifest edit that re-resolved).
    stamp = _wallyStamp()
    for dirName in WALLY_PACKAGE_DIRS:
        if os.path.isdir(dirName):
            with open(os.path.join(dirName, WALLY_STAMP_NAME), "w", encoding="utf-8") as f:
                f.write(stamp + "\n")


def _syncWallyPackages():
    if not os.path.exists(WALLY_MANIFEST):
        return
    if _wallyPackagesFresh():
        return

    if shutil.which("wally") is None and os.path.exists("rokit.toml"):
        runCommand(
            ["rokit", "install"],
            step="Installing project toolchain via Rokit (wally not found on PATH).",
            suggestions=["Run 'rokit install' manually to see detailed errors."],
        )

    installCommand = ["wally", "install"]
    if isCiMode():
        # In CI a stale lockfile must fail loudly, never silently re-resolve
        # to different versions than the ones developers tested against.
        installCommand.append("--locked")
    runCommand(
        installCommand,
        step="Installing wally packages (dependencies missing or out of date).",
        suggestions=[
            "Install wally or ensure it is available on PATH (rokit add wally).",
            "Run 'wally install' manually to see detailed resolution errors.",
        ],
    )

    # Re-export package types for luau-lsp. A failure here leaves the build
    # itself correct (packages are installed), so it warns instead of aborting.
    if shutil.which("wally-package-types") is None:
        announceStep("Skipping package type fixup (wally-package-types not on PATH).")
    else:
        try:
            runCommand(
                ["rojo", "sourcemap", "default.project.json", "--output", "sourcemap.json"],
                step="Generating sourcemap for package type fixup.",
            )
            for dirName in WALLY_PACKAGE_DIRS:
                if os.path.isdir(dirName):
                    runCommand(
                        ["wally-package-types", "--sourcemap", "sourcemap.json", dirName],
                        step=f"Fixing package type re-exports in {dirName}/.",
                    )
        except SGFLError as err:
            warn(f"Package type fixup failed: {err.message} Packages are installed; types may be incomplete.")

    _writeWallyStamps()


def _runRojoBuild():
    _syncWallyPackages()
    runCommand(
        ["rojo", "build", "default.project.json", "-o", "Place.rbxl"],
        step="Compiling scripts via Rojo into Place.rbxl.",
        suggestions=[
            "Install Rojo or ensure it is available on PATH (rokit add rojo).",
            "Verify default.project.json exists and is valid.",
            "Run 'rojo build default.project.json -o Place.rbxl' manually to see detailed errors.",
        ],
    )


# The Rojo plugin's connect dialog defaults to this port, so it is also the
# default `rojo serve` listens on when neither the CLI nor the project file
# says otherwise.
DEFAULT_ROJO_PORT = 34872
ROJO_PORT_SCAN_LIMIT = 10


def _configuredRojoPort() -> int:
    """The port `rojo serve` would pick on its own: default.project.json's
    servePort if pinned there, else Rojo's built-in default."""
    try:
        with open("default.project.json", "r", encoding="utf-8") as f:
            servePort = json.load(f).get("servePort")
        if isinstance(servePort, int):
            return servePort
    except (OSError, ValueError):
        pass  # missing/unreadable project file fails later, in rojo itself
    return DEFAULT_ROJO_PORT


def _portIsFree(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _resolveRojoServePort(requestedPort: Optional[int]) -> Optional[int]:
    """Decide which port to hand `rojo serve`.

    An explicit --port is used exactly as given — no scanning — so a taken
    port fails loudly in rojo rather than silently landing somewhere else.

    With no flag, scan upward from the configured port for a free one. The
    bump must be LOUD, never silent: the Rojo plugin inside Studio defaults
    to the standard port, so a quietly-moved server would leave this Studio
    instance syncing against the *other* project's rojo serve.

    Returns None when the configured port is free and nothing was requested,
    so the plain `rojo serve` invocation (which respects any project-file
    servePort) stays byte-identical to previous versions.
    """
    if requestedPort is not None:
        return requestedPort

    basePort = _configuredRojoPort()
    for offset in range(ROJO_PORT_SCAN_LIMIT):
        candidate = basePort + offset
        if _portIsFree(candidate):
            if offset == 0:
                return None
            warn(
                f"Port {basePort} is already in use (another rojo serve or sgfl start?). "
                f"Serving on port {candidate} instead — set the port to {candidate} in the "
                f"Rojo plugin's connect dialog for THIS Studio instance before connecting."
            )
            return candidate

    raise SGFLError(
        f"No free port found for rojo serve (tried {basePort}-{basePort + ROJO_PORT_SCAN_LIMIT - 1}).",
        suggestions=[
            "Close unused rojo serve processes or Studio sessions.",
            "Pass an explicit port with sgfl start --port <port>.",
        ],
    )


def startPlace(pull: bool, servePort: Optional[int] = None):
    announceStep("Checking environment configuration for publish flow.")
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    executionKey = cloud.getExecutionKey()
    userId = getEnvSafe("USER_ID")

    assetTable = _loadAssetTable()
    _checkForLegacyAssets(assetTable)

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
    try:
        placeBinary = cloud.buildFinalPlace(
            universeId=universeId,
            basePlaceId=placeId,
            executionKey=executionKey,
            publishKey=publishKey,
            downloadKey=downloadKey,
            assetTable=assetTable,
            placeFilePath=PLACE_FILE_PATH,
        )
    finally:
        deleteFile(PLACE_FILE_PATH)

    versionNumber = cloud.uploadPlaceVersion(
        universeId=universeId,
        placeId=placeId,
        publishKey=publishKey,
        data=placeBinary,
        versionType="Published",
    )
    announceStep(f"Published place version {versionNumber}.")

    placeOpenString = f"roblox-studio:1+userId:{userId}+task:EditPlace+placeId:{placeId}+universeId:{universeId}"

    # open studio (generic window)
    # A failure here (e.g. `open`/`start` not working) must not abort the
    # rest of the flow — we still want to open VS Code and start Rojo.
    # Warn and continue instead of propagating.
    try:
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
    except SGFLError as err:
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END} "
            f"Could not open Roblox Studio automatically: {err.message} "
            f"Continuing — open the place manually if needed."
        )

    runCommand(
        ["code", "."],
        step="Opening project in VS Code.",
        captureOutput=False,
        shell=True,
    )
    resolvedPort = _resolveRojoServePort(servePort)
    serveCommand = ["rojo", "serve"]
    if resolvedPort is not None:
        serveCommand += ["--port", str(resolvedPort)]
        announceStep(f"Starting Rojo server on port {resolvedPort}.")
    else:
        announceStep("Starting Rojo server.")
    runCommand(
        serveCommand,
        suggestions=[
            "Install Rojo or ensure it is available on PATH.",
            "Run rojo serve manually to inspect detailed setup issues.",
        ],
        captureOutput=False,
    )


def savePlace():
    announceStep("Checking environment configuration for save flow.")
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    executionKey = cloud.getExecutionKey()

    assetTable = _loadAssetTable()
    _checkForLegacyAssets(assetTable)

    placeVersion = cloud.runProjectionSave(
        universeId=universeId,
        placeId=placeId,
        executionKey=executionKey,
        downloadKey=downloadKey,
        assetTable=assetTable,
    )

    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} "
        f"Saved place version {placeVersion} to the local file system."
    )


def migratePlace():
    announceStep("Checking environment configuration for migration.")
    placeId = getEnvSafe("PLACE_ID")
    universeId = getEnvSafe("UNIVERSE_ID")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    executionKey = cloud.getExecutionKey()

    assetTable = _loadAssetTable()
    legacyFiles = _legacyEntryFiles(assetTable)
    newFiles = [
        f"{spec['folder']}/{name}.sgfl"
        for name, spec in assetTable.items()
        if os.path.isfile(f"{spec['folder']}/{name}.sgfl")
    ]

    if not legacyFiles:
        if newFiles:
            print(
                f"{color.GREEN}{color.BOLD}NOOP{color.END} "
                f"This project is already on the new format ({len(newFiles)} entry files). Nothing to migrate."
            )
            return
        announceStep("No legacy asset files found — treating this as a first-time projection.")

    if legacyFiles:
        if not sys.stdin.isatty():
            raise SGFLError(
                "Refusing to migrate in a non-interactive session.",
                details="sgfl migrate replaces local asset files and requires a typed confirmation.",
                suggestions=["Run sgfl migrate from an interactive terminal."],
            )

        print()
        print(
            f"{color.CYAN}{color.BOLD}INFO{color.END} Migration will project the place "
            f"currently on Roblox (place {placeId}) into the new .sgfl format, then delete "
            f"these {len(legacyFiles)} legacy file(s):"
        )
        for path in legacyFiles:
            print(f"        - {path}")
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END} Any local changes in those files that were "
            f"never published will be lost (git history is your recovery path)."
        )
        print()

        expected = "MIGRATE"
        try:
            entered = input(f'Type "{expected}" to confirm (or anything else to abort): ')
        except EOFError:
            entered = ""
        if entered.strip() != expected:
            announceStep("Aborted by user.")
            raise typer.Exit(code=0)

    placeVersion = cloud.runProjectionSave(
        universeId=universeId,
        placeId=placeId,
        executionKey=executionKey,
        downloadKey=downloadKey,
        assetTable=assetTable,
    )

    for path in legacyFiles:
        deleteFile(path)
    if legacyFiles:
        announceStep(f"Deleted {len(legacyFiles)} legacy entry file(s).")

    if os.path.isfile(getFileURI("postbuild.luau")):
        print(
            f"{color.YELLOW}{color.BOLD}NEXT{color.END} This project has a postbuild.luau hook. "
            f"The new pipeline does not run it — port anything still needed to postapply.luau "
            f"(plain Luau, `return function(game)`, runs inside the cloud session before the "
            f"place is saved). StyleLink re-linking is no longer needed: cross-entry references "
            f"are preserved natively."
        )

    print(
        f"\n{color.GREEN}{color.BOLD}SUCCESS{color.END} "
        f"Migrated to the new format (projected place version {placeVersion}). "
        f"sgfl start / save / publish now work as before."
    )


def _promptInitId(label: str) -> str:
    """Prompt for an optional numeric ID. Blank (Enter) leaves it unset."""
    while True:
        entered = input(f"{label} (numeric, or press Enter to leave blank): ").strip()
        if entered == "":
            return ""
        if not entered.isdigit():
            print(
                f"{color.YELLOW}{color.BOLD}WARN{color.END} "
                f"{label} should be numeric. Try again, or press Enter to leave blank."
            )
            continue
        return entered


def _promptInitConfig() -> dict:
    """Collect project config interactively. Skips prompting in non-TTY sessions
    (returns blanks + a sensible default name) so scaffolding still works in CI."""
    defaultName = os.path.basename(os.getcwd()) or "My Game"
    config = {"name": defaultName, "PLACE_ID": "", "UNIVERSE_ID": ""}

    if not sys.stdin.isatty():
        return config

    announceStep("Configuring your new project. Press Enter to accept defaults / leave blank.")
    enteredName = input(f"Project name [{defaultName}]: ").strip()
    if enteredName:
        config["name"] = enteredName
    config["PLACE_ID"] = _promptInitId("PLACE_ID")
    config["UNIVERSE_ID"] = _promptInitId("UNIVERSE_ID")
    print()
    return config


def initPlace():
    config = _promptInitConfig()

    announceStep("Creating SGFL project folder structure.")
    createDirIfNotExist("src")
    createDirIfNotExist("src/Shared")
    createDirIfNotExist("src/Shared/Util")
    createDirIfNotExist("src/ServerScriptService")
    createDirIfNotExist("src/ReplicatedFirst")
    createDirIfNotExist("src/StarterPlayerScripts")
    createDirIfNotExist("src/StarterCharacterScripts")

    assetTable = getTableFromJsonFile("json/default.assets.json")

    # Entry files are created by the first `sgfl save`; a publish with no
    # entry file for an entry simply keeps the base content, so a fresh
    # project needs only the folders.
    for name, data in assetTable.items():
        createDirIfNotExist(data["folder"])

    # https://stackoverflow.com/a/12309296
    with open("assets.json", "w", encoding="utf-8") as f:
        json.dump(assetTable, f, ensure_ascii=False, indent=4)

    with open("default.project.json", "w", encoding="utf-8") as f:
        fileJson = getTableFromJsonFile("json/default.project.json")
        fileJson["name"] = config["name"]
        json.dump(fileJson, f, ensure_ascii=False, indent=4)

    with open("README.md", "w") as f:
        f.write("Generated by SGFL!\n")

    # Per-developer secrets (PUBLISH_KEY/DOWNLOAD_KEY/USER_ID) live in
    # ~/.sgfl/credentials via `sgfl auth login`, not in the project .env.
    with open(".env", "w") as f:
        f.write(f"PLACE_ID={config['PLACE_ID']}\n")
        f.write(f"UNIVERSE_ID={config['UNIVERSE_ID']}")

    with open(getAbsoluteFileURI("misc/gitignore.txt"), "r") as f:
        text = f.readlines()
        with open(".gitignore", "w") as g:
            g.writelines(text)

    with open(getAbsoluteFileURI("misc/gitattributes.txt"), "r") as f:
        text = f.readlines()
        with open(".gitattributes", "w") as g:
            g.writelines(text)

    runCommand(["rokit", "init"], step="Initializing Rokit toolchain.")
    runCommand(["rokit", "add", "rojo"], step="Adding Rojo to toolchain.")
    runCommand(["rokit", "install"], step="Installing toolchain dependencies.")

    if not os.path.exists(CREDENTIALS_PATH):
        print(
            f"{color.YELLOW}{color.BOLD}NEXT{color.END} "
            f"No developer credentials found at {CREDENTIALS_PATH}. "
            f"Run {color.BOLD}sgfl auth login{color.END} to set your "
            f"PUBLISH_KEY / DOWNLOAD_KEY / EXECUTION_KEY / USER_ID before {color.BOLD}sgfl start{color.END}."
        )

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
    executionKey: Optional[str] = None,
    userId: Optional[str] = None,
):
    interactive = (
        publishKey is None
        and downloadKey is None
        and executionKey is None
        and userId is None
    )

    if interactive:
        if not sys.stdin.isatty():
            raise SGFLError(
                "Refusing to prompt for credentials in a non-interactive session.",
                details="sgfl auth login requires a TTY when called without flags.",
                suggestions=[
                    "Run sgfl auth login from an interactive terminal.",
                    "For scripts, pass --publish-key / --download-key / --execution-key / --user-id directly.",
                ],
            )

        loadCredentials()
        current = {key: os.environ.get(key) for key in CREDENTIAL_KEYS}

        announceStep(f"Updating credentials at {CREDENTIALS_PATH}.")
        print("Press Enter to keep an existing value; type a new value to replace it.\n")

        publishKey = _promptCredential("PUBLISH_KEY", current["PUBLISH_KEY"], hideInput=True)
        downloadKey = _promptCredential("DOWNLOAD_KEY", current["DOWNLOAD_KEY"], hideInput=True)
        executionKey = _promptCredential("EXECUTION_KEY", current["EXECUTION_KEY"], hideInput=True)
        userId = _promptCredential("USER_ID", current["USER_ID"], hideInput=False)

        if publishKey is None and downloadKey is None and executionKey is None and userId is None:
            print(f"\n{color.YELLOW}{color.BOLD}NOOP{color.END} No credentials changed.")
            return

    values = {
        "PUBLISH_KEY": publishKey,
        "DOWNLOAD_KEY": downloadKey,
        "EXECUTION_KEY": executionKey,
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
            "If sgfl was not installed via pipx, reinstall with: pipx install --force git+https://github.com/Stuart-Games/sgfl.git",
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
        if key in ("PUBLISH_KEY", "DOWNLOAD_KEY", "EXECUTION_KEY") and len(trimmed) < 16:
            notes.append("looks unusually short for an API key")

        display = (
            maskSecret(trimmed)
            if key in ("PUBLISH_KEY", "DOWNLOAD_KEY", "EXECUTION_KEY")
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


# ---------------------------------------------------------------------------
# Build / upload split
#
# The cloud apply is slow (minutes), rate-limited (5 task creations/min per key
# owner) and writes Saved versions to a real place. Doing it once per publish
# target — or re-doing it because upload #3 of 5 got a 500 — is what made the
# old single-shot publish flow unusable unattended. `sgfl build` runs it once
# and emits bytes; `sgfl upload` promotes those exact bytes anywhere, cheaply
# and repeatably. `sgfl publish` is now just the two of them back to back.
# ---------------------------------------------------------------------------

PLACE_FILE_MAGIC = b"<roblox!"


def _resolvePublishTargets(placesFilter: Optional[list[str]]) -> tuple[dict, Optional[str]]:
    """Discovered places split into publish targets and the reserved build place."""
    places = discoverPlaceIds()
    buildPlaceId = places.pop(BUILD_PLACE_NAME, None)

    if not places:
        raise SGFLError(
            "No publish targets found in env file.",
            details=(
                f"PLACE_ID_{BUILD_PLACE_NAME.upper()} is reserved as the pipeline's scratch "
                "apply target and is never published to."
            ),
            suggestions=["Add PLACE_ID or PLACE_ID_<NAME> entries for the places you publish to."],
        )

    if placesFilter:
        normalized = [p.strip().lower() for p in placesFilter if p.strip()]
        if BUILD_PLACE_NAME in normalized:
            raise SGFLError(
                f"'{BUILD_PLACE_NAME}' is a reserved place name and cannot be a publish target.",
                details=f"PLACE_ID_{BUILD_PLACE_NAME.upper()} designates the scratch place the cloud apply runs against.",
            )
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

    return places, buildPlaceId


def _resolveBasePlace(targets: dict, buildPlaceId: Optional[str]) -> tuple[str, str]:
    """Which place the apply task runs against.

    buildFinalPlace uploads the asset-less rojo build as a Saved version before
    the apply task runs, so whichever place this returns has an asset-less
    skeleton as its latest version for the duration of the task — and keeps it
    if the task dies. That must not be a live place in an automated run.
    """
    if buildPlaceId:
        return BUILD_PLACE_NAME, buildPlaceId

    if isCiMode():
        raise SGFLError(
            f"PLACE_ID_{BUILD_PLACE_NAME.upper()} is required when SGFL_CI is set.",
            details=(
                "The cloud apply uploads an asset-less Saved base version to its target place "
                "before applying entry files. If the task fails, that skeleton stays the place's "
                "latest version — unacceptable on a live place in an unattended run."
            ),
            suggestions=[
                "Create an empty scratch place in the same universe and set PLACE_ID_BUILD to its ID.",
                "Execution tasks are universe-scoped, so the scratch place must be in UNIVERSE_ID — "
                f"unless you also set {BUILD_UNIVERSE_KEY}, which puts the build place in a "
                "universe of its own.",
            ],
        )

    name = "main" if "main" in targets else sorted(targets.keys())[0]
    print(
        f"{color.YELLOW}{color.BOLD}WARN{color.END} "
        f"No PLACE_ID_{BUILD_PLACE_NAME.upper()} declared — the apply task will run against "
        f"'{name}' ({targets[name]}), leaving an asset-less Saved version on it if it fails. "
        f"Point PLACE_ID_{BUILD_PLACE_NAME.upper()} at a scratch place to avoid this."
    )
    return name, targets[name]


def _resolveBuildTarget(
    targets: Optional[dict] = None,
    buildPlaceId: Optional[str] = None,
) -> tuple[str, str, str, bool]:
    """Where the cloud apply runs: (universeId, placeName, placeId, shared).

    Two shapes.

    With UNIVERSE_ID_BUILD set, the build place lives in a universe of its own
    and the build needs no publish target at all — which is the point: the
    build job can run on nothing but org-level constants, and every credential
    it holds is scoped to a universe containing no game. One such place can
    serve many repos.

    Without it, the build place is a scratch place inside UNIVERSE_ID, resolved
    from the publish targets exactly as before.

    `shared` reports the first shape. A build place that many repos can reach
    is one nothing guarantees you are alone on, so the caller must stop
    inferring which version the apply produced — see cloud.buildFinalPlace's
    strictSaveVersion.
    """
    buildUniverseId = getBuildUniverseId()
    if buildUniverseId:
        placeId = discoverPlaceIds().get(BUILD_PLACE_NAME)
        if not placeId:
            raise SGFLError(
                f"{BUILD_UNIVERSE_KEY} is set but PLACE_ID_{BUILD_PLACE_NAME.upper()} is not.",
                details=(
                    "A dedicated build universe is identified by both values; sgfl will not "
                    "guess which place inside it to apply against."
                ),
                suggestions=[
                    f"Set PLACE_ID_{BUILD_PLACE_NAME.upper()} to a place in universe {buildUniverseId}.",
                    f"Or unset {BUILD_UNIVERSE_KEY} to build against a scratch place in UNIVERSE_ID.",
                ],
            )
        return buildUniverseId, BUILD_PLACE_NAME, placeId, True

    if targets is None:
        targets, buildPlaceId = _resolvePublishTargets(None)
    name, placeId = _resolveBasePlace(targets, buildPlaceId)
    return getEnvSafe("UNIVERSE_ID"), name, placeId, False


PREFLIGHT_KEYS = ("PUBLISH_KEY", "DOWNLOAD_KEY", "EXECUTION_KEY")


def preflight(env: str) -> dict:
    """Check every credential is present and still alive, and nothing else.

    Exists because an expired key is indistinguishable from a permissions
    problem by the time the pipeline hits one: the place-version endpoint
    answers "User unauthorized to update place", several minutes into a build,
    naming neither the key nor the expiry. Run this first and a lapsed key
    fails in seconds, named.

    Deliberately does no work — no rojo, no execution task, no upload — so it
    is free to run on every CI job and cannot itself be the thing that breaks.
    """
    announceStep(f"Loading environment file .env.{env}.")
    loadEnvFile(env)

    universeId, buildPlaceName, buildPlaceId, sharedBuild = _resolveBuildTarget()
    announceStep(f"Checking credentials against universe {universeId}.")

    def resolve(name: str) -> str:
        # EXECUTION_KEY has a legacy ~/.sgfl/execution.key fallback, so reading
        # the environment alone would report a working setup as MISSING.
        if name == "EXECUTION_KEY":
            try:
                return cloud.getExecutionKey()
            except SGFLError:
                return ""
        return (os.environ.get(name) or "").strip()

    resolved = {name: resolve(name) for name in PREFLIGHT_KEYS}
    missing = [name for name in PREFLIGHT_KEYS if not resolved[name]]
    results = []
    for name in PREFLIGHT_KEYS:
        value = resolved[name]
        if not value:
            print(f"  - {name}: {color.RED}MISSING{color.END}")
            continue
        result = cloud.verifyKey(name, value, universeId)
        results.append(result)
        if result["alive"] is True:
            label = f"{color.GREEN}OK{color.END}"
        elif result["alive"] is False:
            label = f"{color.RED}DEAD{color.END}"
        else:
            label = f"{color.YELLOW}UNKNOWN{color.END}"
        print(f"  - {name}: {label} ({maskSecret(value)}) — {result['note']}")

    dead = [r["key"] for r in results if r["alive"] is False]
    if missing or dead:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if dead:
            details.append("rejected by Roblox: " + ", ".join(dead))
        raise SGFLError(
            "Preflight failed — the pipeline would fail later, less clearly.",
            details="; ".join(details),
            suggestions=[
                "A rejected key is usually an expired one. Check its status in "
                "Creator Dashboard -> Open Cloud -> API Keys, regenerate, and update the secret.",
                "Keys created together tend to expire together — check all three, not just the one named.",
            ],
        )

    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} All credentials live. "
        f"Build target: {buildPlaceName} ({buildPlaceId}) in universe {universeId}"
        f"{' [shared]' if sharedBuild else ''}."
    )
    return {
        "env": env,
        "universeId": universeId,
        "buildPlace": {"name": buildPlaceName, "placeId": buildPlaceId, "shared": sharedBuild},
        "keys": results,
    }


def _enforceIntegrity(failOnWarn: bool) -> None:
    """Turn "the output may not match the repo" warnings into a failure.

    Off by default because a human watching the output can judge for
    themselves. Unattended there is nobody to judge, and the warnings that
    matter are exactly the ones that do not stop the build: a checkout without
    git-lfs made every blob entry a pointer stub, the engine skipped all three
    with a warning, and the build was on course to succeed having dropped most
    of the game.
    """
    if not failOnWarn:
        return
    warnings = integrityWarnings()
    if not warnings:
        return
    raise SGFLError(
        f"--fail-on-warn: {len(warnings)} warning(s) mean the output may not match the repo.",
        details="; ".join(warnings),
        suggestions=[
            "Each warning above names what did not make it in — fix that rather than dropping the flag.",
            "Blob entries arriving empty usually means the checkout did not fetch git-lfs objects.",
        ],
    )


def _readArtifact(path: str) -> bytes:
    if not os.path.exists(path):
        raise SGFLError(
            f"Place artifact not found: {path}",
            suggestions=[
                "Run sgfl build <env> --out <path> to produce one.",
                "In CI, confirm the build job's artifact was downloaded before this step.",
            ],
        )
    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(PLACE_FILE_MAGIC):
        raise SGFLError(
            f"{path} is not a binary .rbxl place file.",
            details=f"Expected it to start with {PLACE_FILE_MAGIC!r}; got {data[:16]!r}.",
            suggestions=[
                "Pass the file produced by sgfl build, not a rojo output or an .rbxlx.",
                "A truncated file usually means the CI artifact upload/download was incomplete.",
            ],
        )
    return data


def _artifactDigest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def buildArtifact(env: str, *, outPath: str, noBuild: bool = False, failOnWarn: bool = False) -> dict:
    """Produce final, sidecar-patched place bytes without publishing anything.

    This is the whole expensive half of the pipeline (rojo -> Saved base ->
    cloud apply -> download -> sidecar patch), and it touches only the scratch
    build place. Safe to run on every PR as a validation gate.
    """
    resetIntegrityWarnings()
    announceStep(f"Loading environment file .env.{env}.")
    loadEnvFile(env)

    announceStep("Reading universe configuration.")
    publishKey = getEnvSafe("PUBLISH_KEY")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    executionKey = cloud.getExecutionKey()

    # Deliberately no publish targets here. A build touches the build place and
    # nothing else, so with UNIVERSE_ID_BUILD set it can run without knowing a
    # single game place ID — no env file, no place IDs, no way to reach a game.
    universeId, baseName, basePlaceId, sharedBuild = _resolveBuildTarget()

    assetTable = _loadAssetTable()
    _checkForLegacyAssets(assetTable)

    if noBuild:
        if not os.path.exists(PLACE_FILE_PATH):
            raise SGFLError(
                "--no-build was passed but Place.rbxl does not exist.",
                details=f"Expected file at: {PLACE_FILE_PATH}.",
                suggestions=["Run without --no-build to compile it via rojo first."],
            )
        announceStep(f"Reusing existing build — {_formatPlaceFileSummary(PLACE_FILE_PATH)}.")
    else:
        _runRojoBuild()

    announceStep(
        f"Applying assets via the build place '{baseName}' ({basePlaceId}) "
        f"in universe {universeId}."
    )
    try:
        placeBinary = cloud.buildFinalPlace(
            universeId=universeId,
            basePlaceId=basePlaceId,
            executionKey=executionKey,
            publishKey=publishKey,
            downloadKey=downloadKey,
            assetTable=assetTable,
            placeFilePath=PLACE_FILE_PATH,
            strictSaveVersion=sharedBuild,
        )
    finally:
        # Only clean up a build we made. --no-build means the caller supplied
        # Place.rbxl and expects it to still be there afterwards.
        if not noBuild:
            deleteFile(PLACE_FILE_PATH)

    outDir = os.path.dirname(os.path.abspath(outPath))
    if outDir:
        os.makedirs(outDir, exist_ok=True)
    with open(outPath, "wb") as f:
        f.write(placeBinary)

    _enforceIntegrity(failOnWarn)

    digest = _artifactDigest(placeBinary)
    sizeMb = len(placeBinary) / (1024 * 1024)
    sgflVersion = _getInstalledVersion()
    print(
        f"{color.GREEN}{color.BOLD}SUCCESS{color.END} Wrote {outPath} "
        f"({sizeMb:.2f} MB, sha256 {digest[:16]}..., sgfl {sgflVersion or 'unknown'})."
    )
    return {
        "env": env,
        "universeId": universeId,
        "sgflVersion": sgflVersion,
        "buildPlace": {
            "name": baseName,
            "placeId": basePlaceId,
            "universeId": universeId,
            "shared": sharedBuild,
        },
        "artifact": {"path": outPath, "bytes": len(placeBinary), "sha256": digest},
    }


def _confirmTargets(env: str, summaryLines: list[str], targets: dict, expectPlaces: Optional[list[str]]) -> None:
    """Authorize an upload.

    Interactively that is the typed phrase plus the arrow-key toggle, unchanged.
    Under SGFL_CI there is no human, so authority comes from --expect-places:
    the caller states the exact target set up front and the upload aborts if
    what the env file resolved to differs. A place added to the env file cannot
    then be published to without someone editing the workflow.
    """
    actual = set(targets.keys())

    if expectPlaces is not None:
        expected = {p.strip().lower() for p in expectPlaces if p.strip()}
        if expected != actual:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            details = []
            if missing:
                details.append(f"expected but not resolved: {', '.join(missing)}")
            if extra:
                details.append(f"resolved but not expected: {', '.join(extra)}")
            raise SGFLError(
                "--expect-places does not match the resolved publish targets.",
                details="; ".join(details),
                suggestions=[
                    f"Resolved targets: {', '.join(sorted(actual))}.",
                    "Update --expect-places if the change is intentional, or fix the env file.",
                ],
            )

    if isCiMode():
        if expectPlaces is None:
            raise SGFLError(
                "--expect-places is required when SGFL_CI is set.",
                details="Unattended uploads must state their targets explicitly instead of trusting whatever the env file resolves to.",
                suggestions=[
                    f"Pass --expect-places {','.join(sorted(actual))}.",
                    "Unset SGFL_CI to use the interactive confirmation instead.",
                ],
            )
        print()
        for line in summaryLines:
            print(line)
        announceStep(f"SGFL_CI set and --expect-places matched ({', '.join(sorted(actual))}) — proceeding.")
        return

    confirmPublish(env, summaryLines)
    toggleMessage = (
        f"{color.RED}{color.BOLD}PLEASE MOVE TO YES TO CONFIRM{color.END}"
        f"  (←/→ to choose, Enter to commit)"
    )
    if not confirmToggle(toggleMessage, defaultYes=False):
        announceStep("Aborted by user.")
        raise typer.Exit(code=0)


def _publishPlaceFile(
    *,
    name: str,
    placeId: str,
    universeId: str,
    publishKey: str,
    placeBinary: bytes,
    versionType: str,
) -> dict:
    """Upload the finished bytes to one place. Never raises: a failure here must
    not stop the remaining targets, so the caller gets a result record and
    decides. 429 is retried (nothing was created); other failures are not,
    because a 5xx may still have produced a version."""
    url = (
        f"https://apis.roblox.com/universes/v1/{universeId}"
        f"/places/{placeId}/versions?versionType={versionType}"
    )
    headers = {"x-api-key": publishKey, "Content-Type": "application/octet-stream"}

    announceStep(f"Uploading to {name} ({placeId})...")
    attempt = 0
    while True:
        try:
            res = requests.post(
                url, headers=headers, data=placeBinary, timeout=UPLOAD_TIMEOUT_SECONDS
            )
        except requests.RequestException as exc:
            return {
                "name": name,
                "placeId": placeId,
                "ok": False,
                "reason": f"network error: {exc}",
                "diagnostics": _httpDiagnostics(
                    method="POST",
                    url=url,
                    apiKeyName="PUBLISH_KEY",
                    apiKey=publishKey,
                    error=exc,
                ),
            }

        if res.status_code == 429 and attempt < cloud.MAX_RETRIES:
            delay = cloud._retryDelay(res, attempt)
            print(
                f"{color.YELLOW}{color.BOLD}RETRY{color.END} {name}: rate limited — "
                f"retrying in {delay:.0f}s ({attempt + 1}/{cloud.MAX_RETRIES})."
            )
            time.sleep(delay)
            attempt += 1
            continue
        break

    if res.status_code != 200:
        return {
            "name": name,
            "placeId": placeId,
            "ok": False,
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

    try:
        versionNumber = res.json().get("versionNumber")
    except ValueError:
        versionNumber = None

    versionText = f" as version {versionNumber}" if versionNumber is not None else ""
    print(f"{color.GREEN}{color.BOLD}OK{color.END}   {name} published successfully{versionText}.")
    return {"name": name, "placeId": placeId, "ok": True, "versionNumber": versionNumber}


def _uploadToTargets(
    *,
    env: str,
    universeId: str,
    publishKey: str,
    targets: dict,
    placeBinary: bytes,
    versionType: str,
) -> dict:
    """Upload the same bytes to every target, then report. Returns the result
    record; raises only after every target has been attempted."""
    results = [
        _publishPlaceFile(
            name=name,
            placeId=targets[name],
            universeId=universeId,
            publishKey=publishKey,
            placeBinary=placeBinary,
            versionType=versionType,
        )
        for name in sorted(targets.keys())
    ]
    failures = [r for r in results if not r["ok"]]

    print(f"\n{color.BOLD}Publish summary{color.END} (env={env}, universe={universeId}):")
    nameWidth = max(len(name) for name in targets.keys())
    for result in results:
        marker = (
            f"{color.GREEN}{color.BOLD} OK {color.END}"
            if result["ok"]
            else f"{color.RED}{color.BOLD}FAIL{color.END}"
        )
        line = f"  {marker}  {result['name'].ljust(nameWidth)}  ->  {result['placeId']}"
        if result["ok"]:
            if result.get("versionNumber") is not None:
                line += f"   version {result['versionNumber']}"
        else:
            line += f"   {result['reason']}"
        print(line)

    record = {
        "env": env,
        "universeId": universeId,
        "versionType": versionType,
        # sgfl is the serializer, so which version built the bytes is part of
        # what reproduces them. Pin sgflRef in CI and this tells you to what.
        "sgflVersion": _getInstalledVersion(),
        "artifactSha256": _artifactDigest(placeBinary),
        "artifactBytes": len(placeBinary),
        "places": [
            {k: v for k, v in r.items() if k != "diagnostics"} for r in results
        ],
        "ok": not failures,
    }

    if failures:
        suggestions = [
            "Re-run sgfl upload with the same artifact — successful places are simply republished with identical bytes.",
            "Inspect each failed place's HTTP diagnostics with --detailed.",
            "Confirm UNIVERSE_ID and every PLACE_ID_<NAME> point to the right place.",
        ]
        if any(f.get("statusCode") in (401, 403) for f in failures):
            suggestions.append(
                "PUBLISH_KEY may lack permissions on one or more places — check scopes and allowed-IP settings in the Creator Dashboard."
            )
        raise SGFLError(
            f"{len(failures)} of {len(targets)} place(s) failed to publish.",
            details="; ".join(f"{f['name']}: {f['reason']}" for f in failures),
            suggestions=suggestions,
            diagnostics={f"HTTP diagnostics ({f['name']})": f["diagnostics"] for f in failures},
            record=record,
        )

    print(
        f"\n{color.GREEN}{color.BOLD}SUCCESS{color.END} Published to {len(targets)} place(s) in universe {universeId}."
    )
    return record


def _writeJsonReport(path: Optional[str], record: dict) -> None:
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, sort_keys=True)
    announceStep(f"Wrote machine-readable report to {path}.")


def uploadArtifact(
    env: str,
    *,
    artifactPath: str,
    placesFilter: Optional[list[str]] = None,
    versionType: str = "Published",
    expectPlaces: Optional[list[str]] = None,
    jsonPath: Optional[str] = None,
    failOnWarn: bool = False,
):
    """Promote already-built place bytes to every declared place.

    No rojo, no execution task, no rate limit — just uploads, so a partial
    failure can be retried immediately with the identical artifact.
    """
    resetIntegrityWarnings()
    announceStep(f"Loading environment file .env.{env}.")
    loadEnvFile(env)

    announceStep("Reading universe configuration.")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    targets, _buildPlaceId = _resolvePublishTargets(placesFilter)

    placeBinary = _readArtifact(artifactPath)
    digest = _artifactDigest(placeBinary)

    summaryLines = [
        f"{color.CYAN}{color.BOLD}INFO{color.END} About to publish to universe {universeId} (env={env}):"
    ]
    nameWidth = max(len(name) for name in targets.keys())
    for name in sorted(targets.keys()):
        summaryLines.append(f"        - {name.ljust(nameWidth)}  ->  {targets[name]}")
    summaryLines.append(f"{color.CYAN}{color.BOLD}INFO{color.END} versionType: {versionType}")
    summaryLines.append(
        f"{color.CYAN}{color.BOLD}INFO{color.END} artifact: {artifactPath} "
        f"({len(placeBinary) / (1024 * 1024):.2f} MB, sha256 {digest[:16]}...)"
    )

    _confirmTargets(env, summaryLines, targets, expectPlaces)

    _enforceIntegrity(failOnWarn)

    try:
        record = _uploadToTargets(
            env=env,
            universeId=universeId,
            publishKey=publishKey,
            targets=targets,
            placeBinary=placeBinary,
            versionType=versionType,
        )
    except SGFLError as err:
        _writeJsonReport(jsonPath, err.record or {"env": env, "ok": False})
        raise
    record["artifact"] = {"path": artifactPath, "sha256": digest}
    _writeJsonReport(jsonPath, record)


def publishPlaces(
    env: str,
    *,
    dryRun: bool = False,
    noBuild: bool = False,
    placesFilter: Optional[list[str]] = None,
    versionType: str = "Published",
    expectPlaces: Optional[list[str]] = None,
    jsonPath: Optional[str] = None,
):
    """Interactive one-shot: build the artifact, then upload it.

    Kept as the everyday command. CI should use `sgfl build` + `sgfl upload`
    instead so the expensive half runs once and the bytes that were validated
    are the exact bytes promoted.
    """
    announceStep(f"Loading environment file .env.{env}.")
    loadEnvFile(env)

    announceStep("Reading universe configuration.")
    universeId = getEnvSafe("UNIVERSE_ID")
    publishKey = getEnvSafe("PUBLISH_KEY")
    downloadKey = getEnvSafe("DOWNLOAD_KEY")
    executionKey = cloud.getExecutionKey()

    targets, buildPlaceId = _resolvePublishTargets(placesFilter)
    # The build half may run in a different universe from the upload half; only
    # the uploads use `universeId`.
    buildUniverseId, baseName, basePlaceId, sharedBuild = _resolveBuildTarget(targets, buildPlaceId)

    assetTable = _loadAssetTable()
    _checkForLegacyAssets(assetTable)

    if noBuild and not os.path.exists(PLACE_FILE_PATH):
        raise SGFLError(
            "--no-build was passed but Place.rbxl does not exist.",
            details=f"Expected file at: {PLACE_FILE_PATH}.",
            suggestions=[
                "Run sgfl publish without --no-build to build first.",
                "Run rojo build default.project.json -o Place.rbxl manually if you have a custom build pipeline.",
            ],
        )

    summaryLines = [
        f"{color.CYAN}{color.BOLD}INFO{color.END} About to publish to universe {universeId} (env={env}):"
    ]
    nameWidth = max(len(name) for name in targets.keys())
    for name in sorted(targets.keys()):
        summaryLines.append(f"        - {name.ljust(nameWidth)}  ->  {targets[name]}")
    summaryLines.append(f"{color.CYAN}{color.BOLD}INFO{color.END} versionType: {versionType}")
    summaryLines.append(
        f"{color.CYAN}{color.BOLD}INFO{color.END} apply target (Saved base versions): "
        f"{baseName} ({basePlaceId}) in universe {buildUniverseId}"
    )
    if noBuild:
        summaryLines.append(
            f"{color.CYAN}{color.BOLD}INFO{color.END} {_formatPlaceFileSummary(PLACE_FILE_PATH)} (existing — --no-build)"
        )
    else:
        summaryLines.append(
            f"{color.CYAN}{color.BOLD}INFO{color.END} The place will be built fresh via rojo + the cloud apply pipeline."
        )

    # A dry run does the entire build — rojo, entry-file collection, the cloud
    # apply, the sidecar patch — and stops at the upload. That is the only way
    # it can actually tell you the publish would have worked; the old version
    # returned before the build and validated nothing but env vars.
    if dryRun:
        print()
        for line in summaryLines:
            print(line)
        print(
            f"\n{color.YELLOW}{color.BOLD}DRY-RUN{color.END} building the place file; "
            f"no upload will be performed."
        )
        if not noBuild:
            _runRojoBuild()
        try:
            placeBinary = cloud.buildFinalPlace(
                universeId=buildUniverseId,
                basePlaceId=basePlaceId,
                executionKey=executionKey,
                publishKey=publishKey,
                downloadKey=downloadKey,
                assetTable=assetTable,
                placeFilePath=PLACE_FILE_PATH,
                strictSaveVersion=sharedBuild,
            )
        finally:
            if not noBuild:
                deleteFile(PLACE_FILE_PATH)
        _enforceIntegrity(failOnWarn)
        record = {
            "env": env,
            "universeId": universeId,
            "dryRun": True,
            "ok": True,
            "artifactBytes": len(placeBinary),
            "artifactSha256": _artifactDigest(placeBinary),
            "places": [
                {"name": name, "placeId": targets[name], "ok": None} for name in sorted(targets)
            ],
        }
        _writeJsonReport(jsonPath, record)
        print(
            f"\n{color.YELLOW}{color.BOLD}DRY-RUN{color.END} built {len(placeBinary) / (1024 * 1024):.2f} MB "
            f"successfully; would have published to {len(targets)} place(s)."
        )
        return

    _confirmTargets(env, summaryLines, targets, expectPlaces)

    if not noBuild:
        _runRojoBuild()

    try:
        placeBinary = cloud.buildFinalPlace(
            universeId=buildUniverseId,
            basePlaceId=basePlaceId,
            executionKey=executionKey,
            publishKey=publishKey,
            downloadKey=downloadKey,
            assetTable=assetTable,
            placeFilePath=PLACE_FILE_PATH,
            strictSaveVersion=sharedBuild,
        )
    finally:
        if not noBuild:
            deleteFile(PLACE_FILE_PATH)

    try:
        record = _uploadToTargets(
            env=env,
            universeId=universeId,
            publishKey=publishKey,
            targets=targets,
            placeBinary=placeBinary,
            versionType=versionType,
        )
    except SGFLError as err:
        _writeJsonReport(jsonPath, err.record or {"env": env, "ok": False})
        raise
    _writeJsonReport(jsonPath, record)
