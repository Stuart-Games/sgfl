from pathlib import Path
import os
import json
import subprocess
import shlex
import sys
import shutil
from typing import Optional


class color:
    PURPLE = "\033[95m"
    CYAN = "\033[96m"
    DARKCYAN = "\033[36m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    END = "\033[0m"


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
    ):
        super().__init__(message)
        self.message = message
        self.details = details
        self.suggestions = suggestions or []
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.diagnostics = diagnostics or {}


API_ENV_KEYS = ["PUBLISH_KEY", "DOWNLOAD_KEY"]
ID_ENV_KEYS = ["PLACE_ID", "UNIVERSE_ID", "USER_ID"]


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
        requiredTools.extend(["lune", "rojo", "code"])
        if pullEnabled:
            requiredTools.append("git")
    elif taskName == "save":
        requiredTools.append("lune")
    elif taskName == "init":
        requiredTools.extend(["rokit"])

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
        rawValue = os.getenv(key)

        if rawValue is None:
            lines.append(f"- {key}: MISSING")
            continue

        value = rawValue
        trimmed = value.strip()
        notes: list[str] = []

        if trimmed == "":
            lines.append(f"- {key}: EMPTY")
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

        lines.append(f"- {key}: SET, {details}")

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


def _formatCommand(command: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def runCommand(
    command: list[str],
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


def runLuauFile(name: str):
    fileURI = getAbsoluteFileURI(name)
    absolutePath = getAbsoluteFileURI("")
    runCommand(
        ["lune", "run", fileURI, absolutePath],
        step=f"Running Luau workflow script: {name}",
        suggestions=[
            "Make sure Lune is installed and available in your PATH.",
            "Check assets.json and source files referenced by the script.",
        ],
    )


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
    val = os.getenv(key)
    if val == None or val.strip() == "":
        raise SGFLError(
            f"Missing environment variable: {key}",
            details="Required configuration value is missing from .env.",
            suggestions=[
                f"Add {key}=... to your .env file in the project root.",
                "Restart your command after updating .env.",
            ],
        )
    return val


def deleteFile(path: str):
    if os.path.exists(path):
        os.remove(path)


PLACE_FILE_PATH = getFileURI("Place.rbxl")
ASSET_CONFIG_FILE_PATH = getFileURI("assets.json")
