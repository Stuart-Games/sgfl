from pathlib import Path
import os
import json
import subprocess
import shlex
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
    ):
        super().__init__(message)
        self.message = message
        self.details = details
        self.suggestions = suggestions or []
        self.command = command
        self.stdout = stdout
        self.stderr = stderr


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
        )
    except OSError as exc:
        raise SGFLError(
            "Failed to start external command.",
            details=str(exc),
            suggestions=suggestions
            or ["Verify your shell environment and PATH are configured correctly."],
        )

    if result.returncode != 0:
        raise SGFLError(
            "External command failed.",
            details=f"Command exited with code {result.returncode}.",
            suggestions=suggestions
            or ["Review the command output below and fix the reported issue."],
            command=_formatCommand(command),
            stdout=_clipOutput(result.stdout),
            stderr=_clipOutput(result.stderr),
        )

    return result


def printSgflError(err: SGFLError):
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


PLACE_FILE_PATH = getFileURI("Place.rbxlx")
ASSET_CONFIG_FILE_PATH = getFileURI("assets.json")
