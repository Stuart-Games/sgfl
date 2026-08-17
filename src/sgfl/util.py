from pathlib import Path
import os
import re
import json
import subprocess
import shlex
import sys
import shutil
import time
import dotenv
import requests
import typer
from importlib.metadata import version as _pkgVersion, PackageNotFoundError
from typing import Optional, Union


def _colorEnabled() -> bool:
    """ANSI is for humans at a terminal. CI logs (and piped output) get plain
    text so the escape codes don't end up in build logs and step summaries."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


class color:
    _ON = _colorEnabled()

    PURPLE = "\033[95m" if _ON else ""
    CYAN = "\033[96m" if _ON else ""
    DARKCYAN = "\033[36m" if _ON else ""
    BLUE = "\033[94m" if _ON else ""
    GREEN = "\033[92m" if _ON else ""
    YELLOW = "\033[93m" if _ON else ""
    RED = "\033[91m" if _ON else ""
    BOLD = "\033[1m" if _ON else ""
    UNDERLINE = "\033[4m" if _ON else ""
    END = "\033[0m" if _ON else ""


class SGFLError(Exception):
    def __init__(
        self,
        message: str,
        details: Optional[str] = None,
        suggestions: Optional[list[str]] = None,
        command: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        diagnostics: Optional[dict[str, list[str]]] = None,
        record: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = details
        self.suggestions = suggestions or []
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.diagnostics = diagnostics or {}
        # Machine-readable result attached by partially-successful operations
        # (e.g. 3 of 5 places published) so --json still reports what landed.
        self.record = record


API_ENV_KEYS = ["PUBLISH_KEY", "DOWNLOAD_KEY", "EXECUTION_KEY"]
ID_ENV_KEYS = ["PLACE_ID", "UNIVERSE_ID", "UNIVERSE_ID_BUILD", "USER_ID"]

# Keys the process was started with, captured before any dotenv load. These
# outrank .env.<env> so a CI runner's secrets can't be clobbered by committed
# ID files — see loadEnvFile.
_PROCESS_ENV_KEYS = frozenset(os.environ.keys())

_envSuffix: Optional[str] = None


def isCiMode() -> bool:
    """True when the caller has explicitly declared a non-interactive automated
    run by setting SGFL_CI. Deliberately does NOT key off the generic CI
    variable: publishing is destructive, so the opt-in has to be sgfl-specific
    rather than something a local test runner can switch on by accident."""
    return bool((os.environ.get("SGFL_CI") or "").strip())


def setEnvSuffix(suffix: str):
    global _envSuffix
    _envSuffix = suffix.strip().upper()


# Warnings split into two kinds. Most are informational — a missing env file
# under SGFL_CI, a swept stale file — and a run that emits them is still
# correct. A few mean the artifact may not contain what the repo says it
# should: a blob that failed to deserialize, a sidecar property that could not
# be written, a version number that had to be inferred. Those are `integrity`
# warnings, and --fail-on-warn trips on those and only those.
#
# The distinction is not cosmetic. A checkout without git-lfs turned every
# blob entry into a pointer stub, the engine skipped all three, and the build
# would have gone green having silently dropped most of the game — because
# nothing was watching the warnings.
_integrityWarnings: list[str] = []


def warn(message: str, *, integrity: bool = False) -> None:
    """Print a WARN line, recording it when it bears on whether the output is
    trustworthy. Never raises: deciding what to do about it is the caller's."""
    if integrity:
        _integrityWarnings.append(message)
    print(f"{color.YELLOW}{color.BOLD}WARN{color.END} {message}")


def integrityWarnings() -> list[str]:
    return list(_integrityWarnings)


def resetIntegrityWarnings() -> None:
    _integrityWarnings.clear()


def maskSecret(value: str) -> str:
    visibleChars = 4
    if len(value) <= visibleChars * 2:
        return "*" * len(value)
    return f"{value[:visibleChars]}...{value[-visibleChars:]}"


def _maskSecret(value: str) -> str:
    return maskSecret(value)


def _toolDiagnosticsForTask(taskName: Optional[str], pullEnabled: bool) -> list[str]:
    requiredTools: list[str] = []

    if taskName == "start":
        requiredTools.append("rojo")
        editorCommand = resolveEditorCommand()
        if editorCommand:
            requiredTools.append(editorCommand.split()[0])
        if pullEnabled:
            requiredTools.append("git")
    elif taskName == "preflight":
        pass  # deliberately needs no external tool
    elif taskName in ("publish", "build"):
        requiredTools.append("rojo")
    elif taskName == "init":
        requiredTools.extend(["rokit"])
    elif taskName == "update":
        requiredTools.append("pipx")

    lines: list[str] = []
    for tool in requiredTools:
        path = shutil.which(tool)
        if path:
            lines.append(f"- tool {tool}: FOUND ({path})")
        else:
            lines.append(f"- tool {tool}: MISSING on PATH")

    return lines


def _runtimeDiagnosticsLines(taskName: Optional[str], pullEnabled: bool) -> list[str]:
    lines = [
        f"cwd: {Path.cwd()}",
        f"platform: {sys.platform}",
        f"python: {sys.version.split()[0]} ({sys.executable})",
        f"assets.json present: {'yes' if os.path.exists(ASSET_CONFIG_FILE_PATH) else 'no'}",
        f"Place.rbxl present: {'yes' if os.path.exists(PLACE_FILE_PATH) else 'no'}",
    ]

    lines.extend(_toolDiagnosticsForTask(taskName, pullEnabled))
    return lines


def _getEnvDiagnosticsLines(keys: Optional[list[str]] = None) -> list[str]:
    targetKeys = keys or [*ID_ENV_KEYS, *API_ENV_KEYS]
    lines: list[str] = []

    envPath = getFileURI(".env")
    if os.path.exists(envPath):
        lines.append(f".env file found at: {envPath}")
    else:
        lines.append(f".env file missing at: {envPath}")

    for key in targetKeys:
        effectiveKey = key
        if _envSuffix:
            suffixedKey = f"{key}_{_envSuffix}"
            suffixedVal = os.getenv(suffixedKey)
            if suffixedVal is not None and suffixedVal.strip() != "":
                effectiveKey = suffixedKey
            else:
                lines.append(f"- {suffixedKey}: MISSING (falling back to {key})")

        rawValue = os.getenv(effectiveKey)

        if rawValue is None:
            lines.append(f"- {effectiveKey}: MISSING")
            continue

        value = rawValue
        trimmed = value.strip()
        notes: list[str] = []

        if trimmed == "":
            lines.append(f"- {effectiveKey}: EMPTY")
            continue

        if value != trimmed:
            notes.append("contains leading/trailing whitespace")

        lowered = trimmed.lower()
        placeholderTokens = [
            "changeme",
            "replace",
            "your_",
            "example",
            "placeholder",
            "none",
            "null",
        ]
        if any(token in lowered for token in placeholderTokens):
            notes.append("looks like a placeholder value")

        if key in ID_ENV_KEYS and not trimmed.isdigit():
            notes.append("should be numeric")

        if key in API_ENV_KEYS and len(trimmed) < 16:
            notes.append("looks unusually short for an API key")

        displayValue = _maskSecret(trimmed) if key in API_ENV_KEYS else trimmed
        details = f"value={displayValue} (len={len(trimmed)})"

        if notes:
            details = details + "; notes: " + "; ".join(notes)

        lines.append(f"- {effectiveKey}: SET, {details}")

    return lines


def announceStep(message: str):
    print(f"{color.CYAN}{color.BOLD}INFO{color.END} {message}")


def _clipOutput(text: Optional[str], maxChars: int = 2000) -> Optional[str]:
    if text is None:
        return None

    trimmed = text.strip()
    if not trimmed:
        return None

    if len(trimmed) <= maxChars:
        return trimmed

    return trimmed[:maxChars] + "\n...[output truncated]"


def _formatCommand(command: Union[list, str]) -> str:
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


def runCommand(
    # A plain string is passed through to the shell verbatim (requires
    # shell=True) — used for user-supplied commands like EDITOR_COMMAND.
    command: Union[list, str],
    step: Optional[str] = None,
    suggestions: Optional[list[str]] = None,
    captureOutput: bool = True,
    shell: bool = False,
):
    if step:
        announceStep(step)

    try:
        result = subprocess.run(
            command,
            capture_output=captureOutput,
            text=True,
            shell=shell,
        )
    except FileNotFoundError:
        raise SGFLError(
            f"Missing dependency: {command[0]}",
            details=f"Could not find executable '{command[0]}'.",
            suggestions=suggestions
            or [
                f"Install '{command[0]}' and ensure it is available on PATH.",
                "Re-run the command after installation.",
            ],
            diagnostics={
                "Command diagnostics": [
                    f"requested command: {_formatCommand(command)}",
                    f"cwd: {Path.cwd()}",
                    f"shell mode: {shell}",
                ]
            },
        )
    except OSError as exc:
        raise SGFLError(
            "Failed to start external command.",
            details=str(exc),
            suggestions=suggestions
            or ["Verify your shell environment and PATH are configured correctly."],
            diagnostics={
                "Command diagnostics": [
                    f"requested command: {_formatCommand(command)}",
                    f"cwd: {Path.cwd()}",
                    f"shell mode: {shell}",
                ]
            },
        )

    if result.returncode != 0:
        commandDiagnostics = [
            f"requested command: {_formatCommand(command)}",
            f"cwd: {Path.cwd()}",
            f"shell mode: {shell}",
        ]
        if step:
            commandDiagnostics.append(f"step: {step}")

        executablePath = shutil.which(command[0]) if command else None
        if executablePath:
            commandDiagnostics.append(f"resolved executable: {executablePath}")

        raise SGFLError(
            "External command failed.",
            details=f"Command exited with code {result.returncode}.",
            suggestions=suggestions
            or ["Review the command output below and fix the reported issue."],
            command=_formatCommand(command),
            stdout=_clipOutput(result.stdout),
            stderr=_clipOutput(result.stderr),
            diagnostics={"Command diagnostics": commandDiagnostics},
        )

    return result


def printSgflError(
    err: SGFLError,
    includeEnvDiagnostics: bool = False,
    envDiagnosticKeys: Optional[list[str]] = None,
    includeDetailedDiagnostics: bool = False,
    taskName: Optional[str] = None,
    pullEnabled: bool = False,
):
    print(f"\n{color.RED}{color.BOLD}ERROR{color.END} {err.message}")

    if err.details:
        print(f"Reason: {err.details}")

    if err.command:
        print(f"Command: {err.command}")

    if err.stderr:
        print("\nCommand stderr:")
        print(err.stderr)

    if err.stdout:
        print("\nCommand stdout:")
        print(err.stdout)

    if err.suggestions:
        print("\nHow to fix:")
        for i, suggestion in enumerate(err.suggestions, start=1):
            print(f"  {i}. {suggestion}")

    if includeEnvDiagnostics:
        print("\nDetailed .env diagnostics:")
        for line in _getEnvDiagnosticsLines(envDiagnosticKeys):
            print(f"  {line}")

    if includeDetailedDiagnostics:
        print("\nDetailed runtime diagnostics:")
        for line in _runtimeDiagnosticsLines(taskName, pullEnabled):
            print(f"  {line}")

        for section, lines in err.diagnostics.items():
            if not lines:
                continue
            print(f"\n{section}:")
            for line in lines:
                print(f"  {line}")


def getFileURI(name: str) -> str:
    cwd = Path.cwd()
    return str(cwd) + "/" + name


def getAbsoluteFileURI(name: str) -> str:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(BASE_DIR, name)


def runSilentSubprocess(arr: list[str]):
    runCommand(arr)


def getTableFromJsonFile(filePath: str):
    with open(getAbsoluteFileURI(filePath), "r") as f:
        table = json.load(f)
        return table


def createDirIfNotExist(dirName: str):
    if not os.path.isdir(dirName):
        os.mkdir(dirName)


def getEnvSafe(key: str) -> str:
    if _envSuffix:
        suffixedKey = f"{key}_{_envSuffix}"
        val = os.getenv(suffixedKey)
        if val is not None and val.strip() != "":
            return val
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END}  "
            f'Env suffix "{_envSuffix}" set but {suffixedKey} is not defined — falling back to {key}'
        )

    val = os.getenv(key)
    if val is None or val.strip() == "":
        if key in CREDENTIAL_KEYS:
            suggestions = [
                f"Run sgfl auth login to set {key} (stored at {CREDENTIALS_PATH}).",
                f"Or add {key}=... to your project .env or .env.<env> file to override credentials.",
            ]
        else:
            suggestions = [
                f"Add {key}=... to the .env or .env.<env> file in the project root.",
                "Restart your command after updating the env file.",
            ]
        raise SGFLError(
            f"Missing environment variable: {key}",
            details="Required configuration value is missing.",
            suggestions=suggestions,
        )
    return val


def deleteFile(path: str):
    if os.path.exists(path):
        os.remove(path)


PLACE_FILE_PATH = getFileURI("Place.rbxl")
ASSET_CONFIG_FILE_PATH = getFileURI("assets.json")
CREDENTIALS_DIR = os.path.expanduser("~/.sgfl")
CREDENTIALS_PATH = os.path.join(CREDENTIALS_DIR, "credentials")
CREDENTIAL_KEYS = ["PUBLISH_KEY", "DOWNLOAD_KEY", "EXECUTION_KEY", "USER_ID"]

# Per-developer preferences (v2.11.0) — not secrets, not project config, so
# they live in their own file managed by `sgfl config` rather than squatting
# in credentials. Loaded as the WEAKEST env layer: credentials, .env files,
# and the real process environment all override it. Key -> help text; keys
# outside this table are rejected by `sgfl config set` so typos fail loud.
CONFIG_PATH = os.path.join(CREDENTIALS_DIR, "config")
CONFIG_KEYS = {
    "EDITOR_COMMAND": "Command sgfl start runs to open your editor. 'none' disables the launch; unset means the default ('code .').",
}

EDITOR_COMMAND_KEY = "EDITOR_COMMAND"
DEFAULT_EDITOR_COMMAND = "code ."


def resolveEditorCommand() -> Optional[str]:
    """The command `sgfl start` uses to open an editor, or None when disabled.

    Unset means the historical default (VS Code). 'none' (any case) or an
    empty value disables the launch. Anything else is run as a shell command,
    so a per-developer editor (zed, subl, a wrapper script) needs no project
    change and no flag."""
    value = os.environ.get(EDITOR_COMMAND_KEY)
    if value is None:
        return DEFAULT_EDITOR_COMMAND
    trimmed = value.strip()
    if trimmed == "" or trimmed.lower() == "none":
        return None
    return trimmed

UPDATE_CACHE_PATH = os.path.join(CREDENTIALS_DIR, "update_check.json")
UPDATE_CHECK_TTL_SECONDS = 24 * 60 * 60
UPDATE_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/Stuart-Games/sgfl/main/pyproject.toml"
)
UPDATE_HTTP_TIMEOUT_SECONDS = 3


def _getInstalledVersion() -> Optional[str]:
    try:
        return _pkgVersion("sgfl")
    except PackageNotFoundError:
        return None


def _parseVersionTuple(v: str) -> tuple:
    parts = []
    for chunk in v.strip().split("."):
        m = re.match(r"^(\d+)", chunk)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts)


def _isNewerVersion(remote: str, local: str) -> bool:
    try:
        return _parseVersionTuple(remote) > _parseVersionTuple(local)
    except Exception:
        return False


def _readUpdateCache() -> dict:
    try:
        with open(UPDATE_CACHE_PATH, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _writeUpdateCache(data: dict) -> None:
    try:
        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        with open(UPDATE_CACHE_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetchLatestVersion() -> Optional[str]:
    try:
        res = requests.get(UPDATE_PYPROJECT_URL, timeout=UPDATE_HTTP_TIMEOUT_SECONDS)
    except requests.RequestException:
        return None
    if res.status_code != 200:
        return None
    match = re.search(r'^version\s*=\s*"([^"]+)"', res.text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def invalidateUpdateCache() -> None:
    try:
        if os.path.exists(UPDATE_CACHE_PATH):
            os.remove(UPDATE_CACHE_PATH)
    except OSError:
        pass


def checkForUpdates() -> None:
    """Best-effort version check. Never raises.

    Prints a yellow UPDATE line if a newer sgfl version is on origin/main.
    Cached for 24h in ~/.sgfl/update_check.json. Skipped if SGFL_NO_UPDATE_CHECK
    is set, or if the package isn't installed via metadata (e.g. running from
    an editable source checkout with no version registered).
    """
    try:
        if os.environ.get("SGFL_NO_UPDATE_CHECK"):
            return

        local = _getInstalledVersion()
        if not local:
            return

        cache = _readUpdateCache()
        now = int(time.time())
        lastCheck = int(cache.get("last_check", 0) or 0)
        cachedLatest = cache.get("latest_version")

        if cachedLatest and (now - lastCheck) < UPDATE_CHECK_TTL_SECONDS:
            latest = cachedLatest
        else:
            fetched = _fetchLatestVersion()
            if not fetched:
                return
            latest = fetched
            _writeUpdateCache({"last_check": now, "latest_version": fetched})

        if _isNewerVersion(latest, local):
            print(
                f"{color.YELLOW}{color.BOLD}UPDATE{color.END} "
                f"A newer sgfl is available ({local} -> {latest}). "
                f"Run {color.BOLD}sgfl update{color.END} to upgrade."
            )
    except Exception:
        return


def loadCredentials() -> bool:
    if not os.path.exists(CREDENTIALS_PATH):
        return False
    dotenv.load_dotenv(CREDENTIALS_PATH, override=False)
    return True


def loadConfig() -> bool:
    """Load ~/.sgfl/config as the weakest layer: override=False plus being
    loaded after credentials means anything already set — shell environment
    or credentials — wins over a config-file value."""
    if not os.path.exists(CONFIG_PATH):
        return False
    dotenv.load_dotenv(CONFIG_PATH, override=False)
    return True


def readConfigFile() -> dict:
    values: dict[str, str] = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                values[key.strip()] = val.strip()
    return values


def writeConfigFile(values: dict) -> str:
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    ordered = [key for key in CONFIG_KEYS if key in values]
    ordered += [key for key in values if key not in ordered]
    lines = [f"{key}={values[key]}" for key in ordered]
    with open(CONFIG_PATH, "w") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return CONFIG_PATH


def loadBaseEnv() -> bool:
    envPath = getFileURI(".env")
    if not os.path.exists(envPath):
        return False
    dotenv.load_dotenv(envPath, override=True)
    return True


def saveCredentials(values: dict) -> str:
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)

    existing: dict[str, str] = {}
    if os.path.exists(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH, "r") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                existing[key.strip()] = val.strip()

    for key, value in values.items():
        if value is None:
            continue
        existing[key] = value

    ordered = list(CREDENTIAL_KEYS)
    for key in existing.keys():
        if key not in ordered:
            ordered.append(key)

    lines = []
    for key in ordered:
        if key in existing:
            lines.append(f"{key}={existing[key]}")

    with open(CREDENTIALS_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

    try:
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass

    return CREDENTIALS_PATH


_PLACE_ID_PATTERN = re.compile(r"^PLACE_ID_(.+)$")

# Reserved place name. PLACE_ID_BUILD designates a scratch place used as the
# apply target for the cloud pipeline; it is never a publish destination.
BUILD_PLACE_NAME = "build"

# Optional: put the build place in a universe of its own. Execution tasks are
# universe-scoped, so this is what lets the build credentials be scoped to a
# universe that contains no game — and, because a build then needs no publish
# target at all, what lets one build place serve every repo in an org.
BUILD_UNIVERSE_KEY = "UNIVERSE_ID_BUILD"


def getBuildUniverseId() -> Optional[str]:
    """The dedicated build universe, or None when builds run inside UNIVERSE_ID."""
    value = (os.environ.get(BUILD_UNIVERSE_KEY) or "").strip()
    return value or None


def loadEnvFile(env: str) -> Optional[str]:
    """Load .env.<env> over the already-merged credentials.

    Precedence is real process environment > .env.<env> > ~/.sgfl/credentials.
    dotenv has no notion of "where did this value come from", so the file is
    loaded with override=True and every key the process was actually started
    with is then put back (loudly). Without that, a CI job supplying
    PLACE_ID_LOBBY as a secret would silently publish to whatever ID happened
    to be committed in the env file.

    In SGFL_CI mode a missing env file is not fatal — automated runs are
    expected to supply everything through the environment.
    """
    envFileName = f".env.{env}"
    envPath = getFileURI(envFileName)
    if not os.path.exists(envPath):
        if isCiMode():
            print(
                f"{color.YELLOW}{color.BOLD}WARN{color.END} "
                f"{envFileName} not found — SGFL_CI is set, so continuing with the "
                f"process environment alone."
            )
            return None
        raise SGFLError(
            f"Missing environment file: {envFileName}",
            details=f"Expected to find {envPath}.",
            suggestions=[
                f"Create {envFileName} in the project root with UNIVERSE_ID, PUBLISH_KEY, and one or more PLACE_ID_<NAME> entries.",
                "Run sgfl publish from the directory containing your env files.",
                "In automated runs, set SGFL_CI=1 and supply the values as environment variables instead.",
            ],
        )

    inherited = {key: os.environ[key] for key in _PROCESS_ENV_KEYS if key in os.environ}
    dotenv.load_dotenv(envPath, override=True)

    shadowed = []
    for key, value in inherited.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
            shadowed.append(key)
    if shadowed:
        print(
            f"{color.YELLOW}{color.BOLD}WARN{color.END} "
            f"{envFileName} set {', '.join(sorted(shadowed))}, but the value(s) already in "
            f"the environment take precedence and were kept."
        )

    return envPath


def discoverPlaceIds() -> dict[str, str]:
    discovered: dict[str, str] = {}
    invalid: list[str] = []

    for key, rawValue in os.environ.items():
        match = _PLACE_ID_PATTERN.match(key)
        if not match:
            continue

        name = match.group(1).lower()
        value = (rawValue or "").strip()

        if value == "" or not value.isdigit():
            invalid.append(key)
            continue

        discovered[name] = value

    bare = os.environ.get("PLACE_ID")
    if bare is not None:
        bareValue = bare.strip()
        if bareValue == "" or not bareValue.isdigit():
            invalid.append("PLACE_ID")
        elif "main" in discovered:
            raise SGFLError(
                "Both PLACE_ID and PLACE_ID_MAIN are set — they map to the same place name 'main'.",
                details=f"PLACE_ID={bareValue}, PLACE_ID_MAIN={discovered['main']}",
                suggestions=[
                    "Pick one. PLACE_ID becomes the place named 'main' by default.",
                    "If you want both as distinct entries, rename one (e.g. PLACE_ID_PRIMARY).",
                ],
            )
        else:
            discovered["main"] = bareValue

    if invalid:
        raise SGFLError(
            "One or more place ID entries are invalid.",
            details=f"Invalid keys: {', '.join(sorted(invalid))}.",
            suggestions=[
                "PLACE_ID and every PLACE_ID_<NAME> must be set to a non-empty numeric place ID.",
            ],
        )

    if not discovered:
        raise SGFLError(
            "No place ID entries found in env file.",
            details="Set PLACE_ID and/or one or more PLACE_ID_<NAME> entries.",
            suggestions=[
                "Add PLACE_ID=111111 to your .env.<env> file for the default place.",
                "Add PLACE_ID_<NAME>=... for additional places (becomes a publish target named <name>).",
            ],
        )

    return discovered


def confirmPublish(env: str, summaryLines: list[str]) -> None:
    if not sys.stdin.isatty():
        raise SGFLError(
            "Refusing to prompt for confirmation in a non-interactive session.",
            details="sgfl publish requires a TTY so a human can type the confirmation phrase.",
            suggestions=[
                "Run sgfl publish from an interactive terminal.",
                "If you need to validate config in CI, use --dry-run instead.",
            ],
        )

    print()
    for line in summaryLines:
        print(line)
    print()

    expected = f"PUBLISH {env}"
    prompt = f'Type "{expected}" to confirm (or anything else to abort): '
    try:
        entered = input(prompt)
    except EOFError:
        entered = ""

    if entered.strip() != expected:
        announceStep("Aborted by user.")
        raise typer.Exit(code=0)


def _readKeyFromFd(fd: int) -> str:
    """Read one normalized key from `fd`. Assumes the terminal is already in raw mode.

    Returns one of: 'left', 'right', 'up', 'down', 'enter', 'esc', or the raw character.
    Raises KeyboardInterrupt on Ctrl+C.
    """
    if sys.platform == "win32":
        import msvcrt  # type: ignore

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ch2 = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(ch2, "other")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            return "esc"
        return ch

    import select

    def _read1() -> str:
        return os.read(fd, 1).decode("utf-8", errors="replace")

    ch = _read1()
    if ch == "\x1b":
        if select.select([fd], [], [], 0.1)[0]:
            ch2 = _read1()
            if ch2 == "[" and select.select([fd], [], [], 0.1)[0]:
                ch3 = _read1()
                return {
                    "A": "up",
                    "B": "down",
                    "C": "right",
                    "D": "left",
                }.get(ch3, "other")
        return "esc"
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x03":
        raise KeyboardInterrupt
    return ch


def confirmToggle(message: str, *, defaultYes: bool = False) -> bool:
    """Render an arrow-key NO/YES toggle. Returns True only if user selects YES + Enter.

    Defaults to NO. Left arrow selects NO, right arrow selects YES, Enter commits,
    Esc/Ctrl+C aborts (returns False / raises). Refuses in non-TTY environments.

    The terminal is held in raw mode for the entire interaction so multi-byte
    arrow-key sequences and bare \\r are read correctly between keypresses.
    """
    if not sys.stdin.isatty():
        raise SGFLError(
            "Refusing to prompt for confirmation in a non-interactive session.",
            details="The publish toggle requires a TTY.",
            suggestions=[
                "Run sgfl publish from an interactive terminal.",
                "Use --dry-run to validate config without prompting.",
            ],
        )

    state = {"selected": "yes" if defaultYes else "no"}

    def render():
        noLabel = " NO "
        yesLabel = " YES "
        if state["selected"] == "no":
            noPart = f"{color.BOLD}{color.GREEN}[{noLabel}]{color.END}"
            yesPart = f"[{yesLabel}]"
        else:
            noPart = f"[{noLabel}]"
            yesPart = f"{color.BOLD}{color.RED}[{yesLabel}]{color.END}"
        sys.stdout.write(f"\r\x1b[2K{message}  {noPart}   {yesPart}")
        sys.stdout.flush()

    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()
    try:
        print()
        render()

        if sys.platform == "win32":
            readKey = lambda: _readKeyFromFd(sys.stdin.fileno())
            try:
                while True:
                    key = readKey()
                    if key == "left":
                        state["selected"] = "no"
                        render()
                    elif key == "right":
                        state["selected"] = "yes"
                        render()
                    elif key == "enter":
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return state["selected"] == "yes"
                    elif key == "esc":
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return False
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                sys.stdout.flush()
                raise

        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                key = _readKeyFromFd(fd)
                if key == "left":
                    state["selected"] = "no"
                    render()
                elif key == "right":
                    state["selected"] = "yes"
                    render()
                elif key == "enter":
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return state["selected"] == "yes"
                elif key == "esc":
                    sys.stdout.write("\r\n")
                    sys.stdout.flush()
                    return False
        except KeyboardInterrupt:
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            raise
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    finally:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
