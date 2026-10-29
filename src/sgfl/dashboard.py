"""`sgfl ui` — a localhost dashboard that supervises every sgfl project on the machine.

It owns the long-lived processes (one `sgfl start` / `rojo serve` per project) so
they outlive any chat or terminal, and shells out to the normal CLI for
everything else, so none of the pipeline logic is duplicated here.

Projects are found, not listed: folders registered under "Folders" are scanned
for the sgfl marker pair (assets.json + default.project.json).

What "saved" can and cannot mean: the cloud only ever sees a place version once
Studio has *saved or published* it. Edits sitting unsaved inside a Studio window
are invisible to every API we have. So the dashboard compares the place's latest
cloud version against the version recorded when sgfl last saved.
"""

import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import dotenv
import requests

from .util import CREDENTIALS_DIR, CREDENTIALS_PATH

STATE_PATH = os.path.join(CREDENTIALS_DIR, "ui-state.json")
CONFIG_PATH = os.path.join(CREDENTIALS_DIR, "ui.json")
DEFAULT_ROJO_PORT = 34872
LOG_LINES = 400
VERSION_CACHE_SECONDS = 30
GIT_CACHE_SECONDS = 4
DISCOVERY_DEPTH = 3
_VERSION_PATH = re.compile(r"/versions/(\d+)$")
_LOG_LEVEL = re.compile(r"^(INFO|WARN|ERROR)\s+(.*)")

# Places people keep code. Used once, to seed the folder list on first run.
DEFAULT_ROOT_CANDIDATES = [
    "~/Documents/Code",
    "~/Code",
    "~/Developer",
    "~/Projects",
    "~/dev",
    "~/src",
    "~/repos",
    "~/GitHub",
    "~/Documents/GitHub",
]
SKIP_DIRS = {
    ".git", "node_modules", "Packages", "ServerPackages", "DevPackages",
    ".venv", "venv", "__pycache__", "Library", "dist", "build", "target",
}

# Stages of `sgfl start`, in order. A stage is entered when its marker appears in
# the CLI's output; the first matching marker on a line wins.
START_STAGES = [
    ("build", "Build"),
    ("apply", "Apply in Roblox"),
    ("publish", "Publish"),
    ("studio", "Open Studio"),
    ("serve", "Serve"),
]
_STAGE_MARKERS = [
    (re.compile(r"pulling latest|checking environment|compiling scripts|wally"), 0),
    (re.compile(r"uploading script build|in-engine|postapply"), 1),
    (re.compile(r"downloading applied place|extracting binary sidecar"), 2),
    (re.compile(r"published place version|opening roblox studio"), 3),
    (re.compile(r"starting rojo server"), 4),
]
_SERVING = re.compile(r"rojo server listening")
_PUBLISHED = re.compile(r"published place version (\d+)")
_SAVED_FROM = re.compile(r"extracting binary sidecar from place version (\d+)")
# Past this stage the build place is a script-only skeleton until the apply lands.
_UNSAFE_CANCEL_STAGES = ("apply", "publish")


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def _readJson(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


_jsonLock = threading.RLock()


def _writeJson(path: str, data: dict) -> None:
    os.makedirs(CREDENTIALS_DIR, exist_ok=True)
    with _jsonLock:
        fd, tmp = tempfile.mkstemp(dir=CREDENTIALS_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


def _recordVersion(projectPath: str, version: int) -> None:
    """Remember the place version the repo is known to match (after a save or a start)."""
    with _jsonLock:
        state = _readJson(STATE_PATH)
        state[projectPath] = {"version": version, "at": time.time()}
        _writeJson(STATE_PATH, state)


_portLock = threading.Lock()
_claimedPorts: dict = {}  # port -> project path, for runs that have not bound yet


def _portFree(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


# ---------------------------------------------------------------------------
# Studio awareness
# ---------------------------------------------------------------------------

_studioCache: tuple = (0.0, None)
_windowCache: tuple = (0.0, None)


def studioRunning() -> bool:
    try:
        return subprocess.run(["pgrep", "-xi", "RobloxStudio"], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell — assume running, the safe direction


def studioWindows() -> Optional[list]:
    """Titles of open Roblox Studio windows, or None if we cannot see them.

    Best effort, macOS only: reads window titles through System Events, which needs
    the Accessibility permission. Studio marks a place with unsaved edits with a
    trailing asterisk (UNVERIFIED against a live Studio). A failed probe is retried
    slowly so a pending permission prompt cannot stall every poll.
    """
    global _windowCache
    if not studioRunning():
        return []
    cachedAt, cached = _windowCache
    if time.time() - cachedAt < (10 if cached is not None else 60):
        return cached
    result = None
    if sys.platform == "darwin":
        script = (
            'tell application "System Events" to if exists process "RobloxStudio" then '
            'get name of every window of process "RobloxStudio"'
        )
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3)
            if out.returncode == 0:
                titles = [t.strip() for t in out.stdout.strip().split(", ") if t.strip()]
                result = [{"title": t, "unsaved": bool(re.search(r"\*\s*(-|$)", t))} for t in titles]
        except (OSError, subprocess.SubprocessError):
            result = None
    _windowCache = (time.time(), result)
    return result


def studioSnapshot() -> dict:
    """{running, windows} — computed once per poll, shared by every project."""
    global _studioCache
    cachedAt, cached = _studioCache
    if cached is not None and time.time() - cachedAt < 2:
        return cached
    running = studioRunning()
    snapshot = {"running": running, "windows": studioWindows() if running else []}
    _studioCache = (time.time(), snapshot)
    return snapshot


# ---------------------------------------------------------------------------
# a project and its processes
# ---------------------------------------------------------------------------

class Project:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.name = os.path.basename(self.path)
        self.lock = threading.RLock()
        self.proc: Optional[subprocess.Popen] = None
        self.runId = 0
        self.done = True  # no run in flight
        self.sawVersion: Optional[int] = None
        # `sgfl save` runs beside a serve (that is the normal loop), so it has its own
        # process state rather than sharing the start/serve slot above.
        self.saveProc: Optional[subprocess.Popen] = None
        self.saveRunId = 0
        self.saveDone = True
        self.saveStartedAt = 0.0
        self.saveDetail = ""
        self.saveSeen: Optional[int] = None
        self.kind: Optional[str] = None  # "start" | "serve"
        self.startedAt = 0.0
        self.stage = -1
        self.listening = False
        self.stopRequested = False
        self.port: Optional[int] = None
        self.detail = ""
        self.failure: list = []
        self.last: Optional[dict] = None  # {kind, ok, text, at}
        self.log: deque = deque(maxlen=LOG_LINES)
        self._versionCache: tuple = (0.0, None)
        self._gitCache: tuple = (0.0, {})

    # --- config -----------------------------------------------------------
    def env(self) -> dict:
        values = {}
        if os.path.exists(CREDENTIALS_PATH):
            values.update(dotenv.dotenv_values(CREDENTIALS_PATH))
        envFile = os.path.join(self.path, ".env")
        if os.path.exists(envFile):
            values.update(dotenv.dotenv_values(envFile))
        return values

    def configuredPort(self) -> int:
        try:
            with open(os.path.join(self.path, "default.project.json"), encoding="utf-8") as f:
                return int(json.load(f).get("servePort", DEFAULT_ROJO_PORT))
        except (OSError, ValueError):
            return DEFAULT_ROJO_PORT

    # --- processes --------------------------------------------------------
    def running(self) -> bool:
        return not self.done

    def _observe(self, runId: int, line: str) -> None:
        with self.lock:
            if runId != self.runId:
                return
            self.log.append(line)
            match = _LOG_LEVEL.match(line)
            if match:
                self.detail = match.group(2).strip()
            lowered = line.lower()
            published = _PUBLISHED.search(lowered)
            if published and self.kind == "start":
                # Start just made Roblox match the repo at this version.
                _recordVersion(self.path, int(published.group(1)))
            if self.kind in ("start", "serve") and not self.listening:
                if _SERVING.search(lowered):
                    self.listening = True
                    self.stage = len(START_STAGES)
                elif self.kind == "start":
                    for pattern, stage in _STAGE_MARKERS:
                        if pattern.search(lowered):
                            self.stage = max(self.stage, stage)
                            break

    def _pump(self, runId: int, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            self._observe(runId, raw.rstrip())

    def _wait(self, runId: int, proc: subprocess.Popen, pump: threading.Thread) -> None:
        code = proc.wait()
        # A grandchild (editor, `open`) can hold the pipe open; do not wait on it forever.
        pump.join(timeout=2)
        self._finish(runId, code)

    def _finish(self, runId: int, code: int) -> None:
        with self.lock:
            if runId != self.runId:
                return
            now = time.time()
            if self.stopRequested:
                self.last = {"kind": self.kind, "ok": True, "text": "Stopped", "at": now}
            elif code == 0:
                self.last = {"kind": self.kind, "ok": True, "text": "Exited", "at": now}
            else:
                self.failure = self._failureLines()
                self.last = {
                    "kind": self.kind,
                    "ok": False,
                    "text": self.failure[-1] if self.failure else f"Exited with code {code}",
                    "at": now,
                }
            self.listening = False
            self._releasePort()
            self.done = True

    def _failureLines(self) -> list:
        lines = [l for l in self.log if l.strip()]
        errors = [l for l in lines if l.startswith("ERROR") or "Error" in l[:40]]
        return (errors or lines)[-6:]

    def _claimPort(self) -> int:
        """First free port from the configured one up, skipping ports another run has claimed.

        Nothing is bound during the minutes-long build/apply, so probing alone would hand
        two projects started back to back the same port.
        """
        with _portLock:
            port = self.configuredPort()
            while not _portFree(port) or port in _claimedPorts:
                port += 1
            _claimedPorts[port] = self.path
            self.port = port
            return port

    def _releasePort(self) -> None:
        with _portLock:
            for port in [p for p, owner in _claimedPorts.items() if owner == self.path]:
                del _claimedPorts[port]
        self.port = None

    def _spawn(self, kind: str, args: list) -> None:
        """Caller holds self.lock and has checked that nothing is running."""
        self.runId += 1
        runId = self.runId
        self.kind = kind
        self.startedAt = time.time()
        self.stage = 0 if kind == "start" else -1
        self.listening = False
        self.stopRequested = False
        self.failure = []
        self.detail = ""
        self.sawVersion = None
        self.last = None if self.last and not self.last["ok"] else self.last
        self.log.append(f"$ {' '.join('sgfl' if a == sys.executable else a for a in args)}")
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=self.path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,  # own process group, so Stop also reaches rojo
                env={**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
            )
        except OSError as err:
            hint = " (is rojo on the PATH of the shell that launched `sgfl ui`?)" if args[0] == "rojo" else ""
            self.failure = [f"Could not launch {args[0]}: {err}{hint}"]
            self.last = {"kind": kind, "ok": False, "text": self.failure[0], "at": time.time()}
            self._releasePort()
            self.done = True
            return
        self.done = False
        pump = threading.Thread(target=self._pump, args=(runId, self.proc), daemon=True)
        pump.start()
        threading.Thread(target=self._wait, args=(runId, self.proc, pump), daemon=True).start()

    def startFull(self) -> None:
        """Full `sgfl start`: build, cloud apply, publish, open Studio, then rojo serve."""
        with self.lock:
            if self.running():
                return
            port = self._claimPort()
            self._spawn("start", [sys.executable, "-m", "sgfl.cli", "start", "--port", str(port)])

    def startServe(self) -> None:
        """rojo only: no build, no publish, no Studio."""
        with self.lock:
            if self.running():
                return
            port = self._claimPort()
            self._spawn("serve", ["rojo", "serve", "--port", str(port)])

    def saving(self) -> bool:
        return not self.saveDone

    def startSave(self) -> None:
        """Run `sgfl save`. Allowed while serving, not while a Start is still building: both
        use the one-per-minute execution-task budget and the apply owns the place."""
        with self.lock:
            if self.saving():
                return
            if self.running() and self.kind == "start" and not self.listening:
                return
            self.saveRunId += 1
            runId = self.saveRunId
            self.saveStartedAt = time.time()
            self.saveDetail = ""
            self.saveSeen = None
            if self.last and not self.last["ok"]:
                self.last, self.failure = None, []
            self.log.append("$ sgfl save")
            try:
                self.saveProc = subprocess.Popen(
                    [sys.executable, "-m", "sgfl.cli", "save"],
                    cwd=self.path,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env={**os.environ, "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"},
                )
            except OSError as err:
                self.failure = [f"Could not launch sgfl save: {err}"]
                self.last = {"kind": "save", "ok": False, "text": self.failure[0], "at": time.time()}
                return
            self.saveDone = False
            proc = self.saveProc
        pump = threading.Thread(target=self._pumpSave, args=(runId, proc), daemon=True)
        pump.start()
        threading.Thread(target=self._waitSave, args=(runId, proc, pump), daemon=True).start()

    def _pumpSave(self, runId: int, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:  # type: ignore[union-attr]
            line = raw.rstrip()
            with self.lock:
                if runId != self.saveRunId:
                    return
                self.log.append(line)
                match = _LOG_LEVEL.match(line)
                if match:
                    self.saveDetail = match.group(2).strip()
                saved = _SAVED_FROM.search(line.lower())
                if saved:
                    self.saveSeen = int(saved.group(1))

    def _waitSave(self, runId: int, proc: subprocess.Popen, pump: threading.Thread) -> None:
        code = proc.wait()
        pump.join(timeout=2)
        with self.lock:
            if runId != self.saveRunId:
                return
            now = time.time()
            if code == 0:
                self.last = {"kind": "save", "ok": True, "text": "Saved", "at": now}
                seen = self.saveSeen
                threading.Thread(target=self._recordSaveVersion, args=(seen,), daemon=True).start()
            else:
                self.failure = self._failureLines()
                self.last = {
                    "kind": "save",
                    "ok": False,
                    "text": self.failure[-1] if self.failure else f"Exited with code {code}",
                    "at": now,
                }
            self.saveDone = True

    def stop(self) -> None:
        """Ask nicely first (SIGINT lets sgfl's `finally` blocks run), then escalate."""
        with self.lock:
            if not self.running() or self.proc is None:
                return
            self.stopRequested = True
            runId, proc = self.runId, self.proc
        self._signal(proc, signal.SIGINT)

        def escalate():
            for sig, wait in ((signal.SIGTERM, 8), (signal.SIGKILL, 5)):
                time.sleep(wait)
                if self.runId != runId or self.done:
                    return
                self._signal(proc, sig)

        threading.Thread(target=escalate, daemon=True).start()

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                pass

    def cancelIsRisky(self) -> bool:
        """Between the base upload and the final publish the place holds a script-only skeleton."""
        with self.lock:
            if self.kind != "start" or self.listening or not self.running():
                return False
            return 0 < self.stage <= 2

    def _recordSaveVersion(self, seen: Optional[int]) -> None:
        """Record the version the save task actually projected (from its own log),
        falling back to the latest cloud version when the log did not say."""
        version = seen
        if version is None:
            latest = self.latestVersion(force=True)
            version = latest[0] if latest else None
        if version is not None:
            _recordVersion(self.path, version)
            with self.lock:
                if self.last and self.last["kind"] == "save":
                    self.last["text"] = f"Saved at v{version}"

    # --- status -----------------------------------------------------------
    def latestVersion(self, force: bool = False) -> Optional[tuple]:
        """(version, createTime) of the newest place version, or None."""
        cachedAt, cached = self._versionCache
        if not force and time.time() - cachedAt < VERSION_CACHE_SECONDS:
            return cached
        env = self.env()
        placeId, key = env.get("PLACE_ID"), env.get("DOWNLOAD_KEY")
        result = None
        if placeId and key:
            try:
                response = requests.get(
                    f"https://apis.roblox.com/assets/v1/assets/{placeId}/versions",
                    headers={"x-api-key": key},
                    params={"maxPageSize": 50},
                    timeout=15,
                )
                best = None
                for entry in response.json().get("assetVersions") or []:
                    match = _VERSION_PATH.search(entry.get("path") or "")
                    if match and (best is None or int(match.group(1)) > best[0]):
                        best = (int(match.group(1)), entry.get("createTime"))
                result = best
            except (requests.RequestException, ValueError):
                result = None
        self._versionCache = (time.time(), result)
        return result

    def gitInfo(self) -> dict:
        """Branch, upstream divergence and dirty count from one `git status -sb`."""
        cachedAt, cached = self._gitCache
        if time.time() - cachedAt < GIT_CACHE_SECONDS:
            return cached
        info: dict = {}
        try:
            out = subprocess.run(["git", "status", "-sb"], cwd=self.path, capture_output=True, text=True, timeout=10)
            if out.returncode == 0:
                lines = out.stdout.splitlines()
                head = lines[0][3:] if lines else ""
                ahead = re.search(r"ahead (\d+)", head)
                behind = re.search(r"behind (\d+)", head)
                info = {
                    "branch": head.split("...")[0].split(" [")[0],
                    "hasUpstream": "..." in head,
                    "ahead": int(ahead.group(1)) if ahead else 0,
                    "behind": int(behind.group(1)) if behind else 0,
                    "dirty": len([l for l in lines[1:] if l.strip()]),
                }
        except (OSError, subprocess.SubprocessError):
            info = {}
        self._gitCache = (time.time(), info)
        return info

    def studioOpen(self, snapshot: dict) -> Optional[bool]:
        """Is a Studio window open for this project? None = cannot rule it out.

        A title carries the place name, which can differ from the repo name, so a
        running Studio whose titles do not name this project is treated as unknown,
        never as safe. Only "no Studio process at all" is proof.
        """
        if not snapshot["running"]:
            return False
        windows = snapshot["windows"]
        if windows is None:
            return None
        squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
        if any(squash(self.name) in squash(w["title"]) for w in windows):
            return True
        return None

    def phase(self) -> str:
        with self.lock:
            return self._phase()

    def _phase(self) -> str:
        if self.running():
            return "serving" if self.listening else "starting"
        if self.saving():
            return "saving"
        if self.last and not self.last["ok"]:
            return "failed"
        return "idle"

    def status(self, snapshot: dict) -> dict:
        latest = self.latestVersion()
        saved = _readJson(STATE_PATH).get(self.path)
        if latest is None:
            sync = "unknown"
        elif saved is None:
            sync = "never-saved"
        elif latest[0] > saved["version"]:
            sync = "behind"
        else:
            sync = "in-sync"
        with self.lock:
            phase = self._phase()
            kind, stage = self.kind, self.stage
            saving, saveStartedAt, saveDetail = self.saving(), self.saveStartedAt, self.saveDetail
            log, failure, last = list(self.log)[-120:], list(self.failure), self.last and dict(self.last)
            detail, port, startedAt = self.detail, self.port, self.startedAt
        stages = []
        failedStart = phase == "failed" and last and last["kind"] == "start"
        if (kind == "start" and phase == "starting") or failedStart:  # the rail is for in-flight and broken runs; a finished one just serves
            # a run that died after serving began has stage == len(START_STAGES): blame the last one
            current = min(stage, len(START_STAGES) - 1)
            for i, (key, label) in enumerate(START_STAGES):
                if phase == "serving" or i < current:
                    state = "done"
                elif i == current:
                    state = "failed" if phase == "failed" else "active"
                else:
                    state = "todo"
                stages.append({"key": key, "label": label, "state": state})
        return {
            "path": self.path,
            "name": self.name,
            "phase": phase,
            "kind": kind if phase in ("starting", "serving") else None,
            "saving": saving,
            "saveStartedAt": saveStartedAt if saving else None,
            "startedAt": (saveStartedAt if phase == "saving" else startedAt) if phase in ("starting", "serving", "saving") else None,
            "port": port if phase in ("starting", "serving") else None,
            "cancelRisky": self.cancelIsRisky(),
            "defaultPort": self.configuredPort(),
            "stages": stages,
            "detail": saveDetail if phase == "saving" else (detail if phase == "starting" else ""),
            "failure": failure if phase == "failed" else [],
            "last": last,
            "studioOpen": self.studioOpen(snapshot),
            "git": self.gitInfo(),
            "sync": sync,
            "cloudVersion": latest[0] if latest else None,
            "cloudTime": latest[1] if latest else None,
            "savedVersion": saved["version"] if saved else None,
            "savedAt": saved["at"] if saved else None,
            "log": log,
        }


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def discover(roots: list) -> list:
    """Paths of sgfl projects (assets.json + default.project.json) under the roots."""
    found = []
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        rootDepth = root.rstrip(os.sep).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            if "assets.json" in filenames and "default.project.json" in filenames:
                real = os.path.realpath(dirpath)
                if real not in seen:
                    seen.add(real)
                    found.append(dirpath)
                dirnames[:] = []  # a project's subfolders are its own business
                continue
            depth = dirpath.rstrip(os.sep).count(os.sep) - rootDepth
            if depth >= DISCOVERY_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
    return sorted(found, key=lambda p: os.path.basename(p).lower())


class Registry:
    """Folders to scan, the projects found in them, and which are hidden."""

    def __init__(self, extraRoots: list):
        self.lock = threading.Lock()
        self.cfg = threading.RLock()  # guards config read-modify-write across request threads
        config = _readJson(CONFIG_PATH)
        if "roots" not in config:
            config["roots"] = [os.path.expanduser(r) for r in DEFAULT_ROOT_CANDIDATES if os.path.isdir(os.path.expanduser(r))]
            config.setdefault("hidden", [])
            config.setdefault("pinned", [])
            _writeJson(CONFIG_PATH, config)
        self.config = config
        self.extraRoots = [os.path.abspath(os.path.expanduser(r)) for r in extraRoots]
        self.projects: dict = {}
        self.rescan()

    def roots(self) -> list:
        configured = list(self.config.get("roots", []))
        return configured + [r for r in self.extraRoots if r not in configured]

    def rescan(self) -> None:
        paths = discover(self.roots())
        with self.lock:
            fresh = {p: self.projects.get(p) or Project(p) for p in paths}
            # a project that is running must not vanish because a rescan missed it
            for p, proj in self.projects.items():
                if p not in fresh and proj.running():
                    fresh[p] = proj
            self.projects = fresh

    def _save(self) -> None:
        _writeJson(
            CONFIG_PATH,
            {
                "roots": self.config.get("roots", []),
                "hidden": self.config.get("hidden", []),
                "pinned": self.config.get("pinned", []),
            },
        )

    def addRoot(self, path: str) -> Optional[str]:
        path = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isdir(path):
            return f"Not a folder: {path}"
        with self.cfg:
            if path not in self.config["roots"]:
                self.config["roots"] = self.config["roots"] + [path]
                self._save()
        self.rescan()
        return None

    def removeRoot(self, path: str) -> None:
        with self.cfg:
            self.config["roots"] = [r for r in self.config["roots"] if r != path]
            self._save()
        self.rescan()

    def setHidden(self, path: str, hidden: bool) -> Optional[str]:
        """Hide or show a project. A running project cannot be hidden: it would keep
        its port with no Stop control left on screen."""
        project = self.get(path)
        if hidden and project is not None and project.running():
            return "Stop this project before hiding it."
        with self.cfg:
            current = [h for h in self.config.get("hidden", []) if h != path]
            if hidden:
                current.append(path)
            self.config["hidden"] = current
            self._save()
        return None

    def setPinned(self, path: str, pinned: bool) -> None:
        with self.cfg:
            current = [x for x in self.config.get("pinned", []) if x != path]
            if pinned:
                current.append(path)
            self.config["pinned"] = current
            self._save()

    def isHidden(self, path: str) -> bool:
        return path in self.config.get("hidden", [])

    def get(self, path: str) -> Optional[Project]:
        with self.lock:
            return self.projects.get(path)

    def view(self) -> dict:
        snapshot = studioSnapshot()
        hidden = set(self.config.get("hidden", []))
        pinned = set(self.config.get("pinned", []))
        with self.lock:
            projects = list(self.projects.values())
        shown = [dict(p.status(snapshot), pinned=p.path in pinned) for p in projects if p.path not in hidden]
        shown.sort(key=lambda x: (not x["pinned"], x["name"].lower()))  # pinned first, stable by name
        return {
            "projects": shown,
            "hidden": [{"path": p.path, "name": p.name} for p in projects if p.path in hidden],
            "roots": self.roots(),
            "configuredRoots": self.config.get("roots", []),
            "studio": snapshot,
            "canPick": sys.platform == "darwin",
        }


def pickFolder() -> Optional[str]:
    """Native folder chooser (macOS). Returns None if cancelled or unavailable."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Folder to scan for sgfl projects")'],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip().rstrip("/") or None


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>sgfl</title>
<style>
:root{
  --bg:oklch(96.5% .006 85);--surface:oklch(99% .003 85);--sunk:oklch(94% .008 85);
  --ink:oklch(23% .02 255);--mut:oklch(48% .02 255);--line:oklch(89% .01 85);--line-strong:oklch(80% .012 85);
  --accent:oklch(50% .11 200);--accent-ink:oklch(98% .01 200);
  --ok:oklch(46% .11 150);--warn:oklch(48% .12 70);--warn-soft:oklch(95% .05 85);
  --bad:oklch(47% .17 25);--bad-soft:oklch(95% .03 25);
  --log-bg:oklch(21% .02 255);--log-fg:oklch(86% .01 255);
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
}
@media(prefers-color-scheme:dark){:root{
  --bg:oklch(17% .012 255);--surface:oklch(21% .014 255);--sunk:oklch(15% .012 255);
  --ink:oklch(94% .006 85);--mut:oklch(72% .015 255);--line:oklch(30% .015 255);--line-strong:oklch(40% .018 255);
  --accent:oklch(74% .11 200);--accent-ink:oklch(18% .02 200);
  --ok:oklch(78% .12 150);--warn:oklch(80% .12 80);--warn-soft:oklch(30% .05 80);
  --bad:oklch(75% .13 25);--bad-soft:oklch(29% .05 25);--log-bg:oklch(12% .01 255)}}
*{box-sizing:border-box}
html{color-scheme:light dark}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans);font-variant-numeric:tabular-nums}
::selection{background:var(--accent);color:var(--accent-ink)}
main{max-width:860px;margin:0 auto;padding:28px 16px 64px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
button{font:inherit;color:inherit;cursor:pointer}
svg{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}

header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.brand{font:600 17px/1 var(--mono);letter-spacing:-.02em}
.tools{display:flex;align-items:center;gap:18px;color:var(--mut);font-size:13px}
.link{background:none;border:0;padding:4px 2px;color:var(--mut);display:inline-flex;align-items:center;gap:6px;border-radius:6px}
.link:hover{color:var(--ink)}
.studio-state{display:inline-flex;align-items:center;gap:7px}

.list{background:var(--surface);border:1px solid var(--line);border-radius:12px}
.row{padding:20px 22px}
.row+.row{border-top:1px solid var(--line)}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.name{font-size:17px;font-weight:650;letter-spacing:-.01em;margin:0}
.meta{display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:3px;color:var(--mut);font-size:12.5px}
.mono{font-family:var(--mono);font-size:12px}
.meta>span{display:inline-flex;align-items:center;gap:5px}
.meta .warn{color:var(--warn)}
.state{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;white-space:nowrap;padding-top:2px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut);display:inline-block}
.state[data-phase=serving] .dot{background:var(--ok)}
.state[data-phase=starting] .dot,.state[data-phase=saving] .dot{background:var(--warn);animation:pulse 1.4s ease-in-out infinite}
.state[data-phase=failed] .dot{background:var(--bad)}
.state[data-phase=serving]{color:var(--ok)}.state[data-phase=failed]{color:var(--bad)}
@keyframes pulse{50%{opacity:.35;transform:scale(.8)}}
@media(prefers-reduced-motion:reduce){.state .dot,.rail li[data-state=active] .fill{animation:none!important}.fill{transition:none!important}}

.say{margin:14px 0 0;height:42px;max-width:62ch}
.say .l1,.say .l2{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.say .l1{font-weight:650;font-size:14px;line-height:21px}
.say .l2{color:var(--mut);font-size:12.5px;line-height:19px}

.slot{height:30px;margin-top:10px}
.rail{display:grid;grid-template-columns:repeat(5,1fr);gap:6px;margin:0;padding:0;list-style:none}
.rail li{font-size:12px;color:var(--mut)}
.rail .bar{display:block;height:4px;border-radius:2px;background:var(--line);margin-bottom:6px;overflow:hidden}
.rail .fill{display:block;height:100%;width:0;background:var(--accent);transition:width .3s cubic-bezier(.2,.7,.2,1)}
.rail li[data-state=done] .fill{width:100%}
.rail li[data-state=done]{color:var(--ink)}
.rail li[data-state=active] .fill{width:100%;background:linear-gradient(90deg,var(--accent) 0 30%,transparent 30% 50%,var(--accent) 50% 80%,transparent 80%);background-size:200% 100%;animation:slide 1.2s linear infinite}
.rail li[data-state=active]{color:var(--ink);font-weight:600}
.rail li[data-state=failed] .fill{width:100%;background:var(--bad)}
.rail li[data-state=failed]{color:var(--bad);font-weight:600}
@keyframes slide{to{background-position:-200% 0}}

.fail{margin:14px 0 0;padding:10px 12px;border-radius:8px;background:var(--bad-soft);color:var(--bad);font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
.confirm{flex:1;min-height:56px;padding:6px 14px;border-radius:9px;background:var(--warn-soft);display:flex;gap:10px 16px;align-items:center;justify-content:space-between}
.confirm p{margin:0;font-size:13px;line-height:1.35}
.confirm .btns{display:flex;gap:8px}

.actions{display:flex;flex-wrap:wrap;align-items:center;gap:10px 28px;margin-top:12px;height:56px}
.group{display:flex;gap:8px;align-items:center}
.group+.group{padding-left:28px;border-left:1px solid var(--line)}
.btn{display:inline-flex;flex-direction:column;align-items:flex-start;justify-content:center;min-height:44px;padding:6px 16px;border-radius:9px;border:1px solid var(--line-strong);background:var(--surface);text-align:left;line-height:1.25}
.btn .t{display:inline-flex;align-items:center;gap:7px;font-weight:650}
.btn .s{font-size:11.5px;color:var(--mut);font-weight:400}
.btn:hover:not(:disabled){background:var(--sunk)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.primary .s{color:inherit;opacity:.85}
.btn.primary:hover:not(:disabled){background:var(--accent);filter:brightness(1.08)}
.btn:not(.quiet){width:140px;height:44px}
.group .btn.quiet{width:132px;height:44px;justify-content:center}
.confirm .btn.quiet{width:124px;height:36px;justify-content:center}
.btn.quiet{min-height:36px;flex-direction:row;align-items:center;gap:7px;padding:4px 12px;font-weight:600;font-size:13px;border-color:var(--line)}
.btn.danger{color:var(--bad);border-color:currentColor;background:transparent}
.btn:disabled{opacity:.45;cursor:not-allowed}
.disclosure{margin-top:14px}
.disclosure>summary{cursor:pointer;color:var(--mut);font-size:12.5px;list-style:none;display:inline-flex;align-items:center;gap:6px;border-radius:6px;padding:2px 0}
.disclosure>summary::-webkit-details-marker{display:none}
.disclosure>summary::before{content:"";width:6px;height:6px;border-right:1.6px solid currentColor;border-bottom:1.6px solid currentColor;transform:rotate(-45deg);transition:transform .15s}
.disclosure[open]>summary::before{transform:rotate(45deg)}
pre{background:var(--log-bg);color:var(--log-fg);padding:12px 14px;border-radius:8px;max-height:240px;overflow:auto;font:12px/1.55 var(--mono);margin:8px 0 0;white-space:pre-wrap;word-break:break-word;scrollbar-color:var(--line-strong) transparent}
.hr{display:flex;align-items:center;gap:6px}
.icon{width:32px;height:32px;display:inline-grid;place-items:center;border:0;border-radius:8px;background:transparent;color:var(--mut)}
.icon:hover:not(:disabled){background:var(--sunk);color:var(--ink)}
.icon.on{color:var(--warn)}.icon.on svg{fill:currentColor}
.icon:disabled{opacity:.35;cursor:not-allowed}
.hidden-list{margin-top:14px}

.panel{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin-bottom:14px}
.panel h2{font-size:14px;margin:0 0 4px}
.panel p.hint{margin:0 0 12px;color:var(--mut);font-size:12.5px;max-width:64ch}
.roots{list-style:none;margin:0 0 12px;padding:0}
.roots li{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line)}
.roots li:last-child{border-bottom:0}
.add{display:flex;gap:8px;flex-wrap:wrap}
.add input{flex:1;min-width:220px;font:12.5px var(--mono);padding:8px 10px;border-radius:8px;border:1px solid var(--line-strong);background:var(--bg);color:var(--ink)}
.err{color:var(--bad);font-size:12.5px;margin:8px 0 0}
.empty{padding:36px 22px;text-align:center;color:var(--mut)}
.empty b{display:block;color:var(--ink);font-size:15px;margin-bottom:4px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
@media(max-width:560px){.confirm{flex-wrap:wrap}.group+.group{padding-left:0;border-left:0}.rail li{font-size:11px}.row{padding:18px 16px}}
</style></head><body>
<main>
<header>
  <div class="brand">sgfl</div>
  <div class="tools"><span class="studio-state" id="studio"></span>
  <button class="link" data-act="folders" data-key="folders"><svg viewBox="0 0 16 16"><path d="M2 4.5A1.5 1.5 0 0 1 3.5 3h2.8l1.4 1.5h4.8A1.5 1.5 0 0 1 14 6v5.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 11.5z"/></svg>Folders</button></div>
</header>
<div id="folders"></div>
<div id="app"></div>
<div class="sr" role="status" aria-live="polite" id="live"></div>
</main>
<script>
const TOKEN="__TOKEN__";
const ui={hiddenOpen:false,confirm:new Set(),cancel:new Set(),logOpen:new Map(),scroll:new Map(),folders:false,rootError:""};
let data=null,lastHtml="",lastFolders=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const I={
 branch:'<svg viewBox="0 0 16 16"><circle cx="4.5" cy="3.5" r="1.5"/><circle cx="4.5" cy="12.5" r="1.5"/><circle cx="11.5" cy="5.5" r="1.5"/><path d="M4.5 5v6M11.5 7c0 2.5-3 2.5-7 4"/></svg>',
 play:'<svg viewBox="0 0 16 16"><path d="M5 3.5v9l7-4.5z"/></svg>',
 save:'<svg viewBox="0 0 16 16"><path d="M8 2.5v8M4.5 7.5 8 11l3.5-3.5M3 13.5h10"/></svg>',
 stop:'<svg viewBox="0 0 16 16"><rect x="4" y="4" width="8" height="8" rx="1"/></svg>',
 serve:'<svg viewBox="0 0 16 16"><path d="M2.5 5.5h11M2.5 10.5h11"/></svg>',
 pin:'<svg viewBox="0 0 16 16"><path d="M8 2l1.8 3.7 4 .6-2.9 2.8.7 4L8 11.2 4.4 13.1l.7-4L2.2 6.3l4-.6z"/></svg>',
 hide:'<svg viewBox="0 0 16 16"><path d="M2 8s2.2-4 6-4 6 4 6 4-2.2 4-6 4-6-4-6-4zM3 13 13 3"/></svg>',
};
const fmtClock=s=>{s=Math.max(0,s|0);return (s/60|0)+":"+String(s%60).padStart(2,"0")};
const ago=t=>{if(!t)return"";const s=(Date.now()/1000-t)|0;return s<60?"just now":s<3600?(s/60|0)+"m ago":s<86400?(s/3600|0)+"h ago":(s/86400|0)+"d ago"};
const agoIso=t=>t?ago(Date.parse(t)/1000):"";

async function post(path,body){
  const r=await fetch(path,{method:"POST",headers:{"X-Token":TOKEN,"Content-Type":"application/json"},body:JSON.stringify(body||{})});
  return r.json().catch(()=>({}));
}
async function act(path,action){await post("/api/action",{path,action});await poll()}

function lines(p){
  const clock=`<span data-since="${p.startedAt}"></span>`;
  if(p.saving&&p.phase==="serving")return ["Saving from Roblox into the repo",`<span data-since="${p.saveStartedAt}"></span>${p.detail?" · "+esc(p.detail):""} · still serving on port <span class=mono>${p.port}</span>`];
  if(p.phase==="starting"){
    const cur=p.stages.find(x=>x.state==="active");
    const tail=cur&&cur.key==="apply"?"applying assets in Roblox, usually 1 to 3 min":(p.detail||"working");
    return [`Starting: ${esc(cur?cur.label:"preparing")}`,`${clock} · ${esc(tail)}`];
  }
  if(p.phase==="saving")return ["Saving from Roblox into the repo",`${clock}${p.detail?" · "+esc(p.detail):""}`];
  if(p.phase==="serving"&&p.last&&p.last.kind==="save")return [`Serving on port <span class=mono>${p.port}</span>`,p.last.ok?`${esc(p.last.text)}, ${ago(p.last.at)}`:`Last save failed: ${esc(p.last.text)}`];
  if(p.phase==="serving")return [`Serving on port <span class=mono>${p.port}</span>`,p.port!==34872?`Connect Studio's Rojo plugin to port <span class=mono>${p.port}</span>, not the default`:"Connect from Studio's Rojo plugin"];
  if(p.phase==="failed"){
    const bad=p.stages.find(x=>x.state==="failed");
    return [`${p.last.kind==="save"?"Save":"Start"} failed${bad?" at "+esc(bad.label):""}`,esc(p.last.text||"")];
  }
  if(p.sync==="behind")return ["Roblox has changes the repo doesn't",`Place v${p.cloudVersion}, last save v${p.savedVersion}. Save to pull them in.`];
  if(p.sync==="in-sync")return [`Repo matches Roblox (v${p.cloudVersion})`,p.last&&p.last.ok?`${esc(p.last.text)}, ${ago(p.last.at)}`:"Nothing to save"];
  if(p.sync==="never-saved")return ["No save recorded yet",`Roblox is at v${p.cloudVersion}`];
  return ["Can't read the place version","Needs PLACE_ID in .env and a DOWNLOAD_KEY with Assets read"];
}
function sentence(p){const [a,b]=lines(p);return `<span class=l1>${a}</span><span class=l2>${b}</span>`}
const stateLabel=p=>({idle:"Idle",starting:"Starting",serving:"Serving",saving:"Saving",failed:"Failed"}[p.phase]);
function meta(p){
  const g=p.git||{},out=[];
  if(g.branch){
    out.push(`<span>${I.branch}<span class=mono>${esc(g.branch)}</span></span>`);
    if(g.hasUpstream&&(g.ahead||g.behind))out.push(`<span class=warn>${g.ahead?g.ahead+" ahead":""}${g.ahead&&g.behind?", ":""}${g.behind?g.behind+" behind":""}</span>`);
    else if(!g.hasUpstream)out.push(`<span>no upstream</span>`);
    out.push(g.dirty?`<span class=warn>${g.dirty} uncommitted</span>`:`<span>clean</span>`);
  }
  if(p.cloudVersion)out.push(`<span class=mono>v${p.cloudVersion}${p.cloudTime?" · "+agoIso(p.cloudTime):""}</span>`);
  return out.join("");
}
function confirmStrip(p){
  const path=esc(p.path);
  const why=p.studioOpen===true?"A Studio for this place looks open. Start overwrites it; unsaved edits there are lost.":"Studio is running and may have this place open. Start overwrites it; unsaved edits there are lost.";
  return `<div class=confirm role=group aria-label="Confirm start"><p>${why}</p><div class=btns>
   <button class="btn quiet" data-act=cancel data-path="${path}" data-key="cancel:${path}">Cancel</button>
   <button class="btn quiet danger" data-act=startNow data-path="${path}" data-key="go:${path}">Start anyway</button></div></div>`;
}
function cancelStrip(p){
  const path=esc(p.path);
  return `<div class=confirm role=group aria-label="Confirm cancel"><p>Cancelling now can leave the place on a script-only build.</p><div class=btns>
   <button class="btn quiet" data-act=keep data-path="${path}" data-key="keep:${path}">Keep going</button>
   <button class="btn quiet danger" data-act=stopNow data-path="${path}" data-key="cancelgo:${path}">Cancel start</button></div></div>`;
}
function dupes(p){
  if(data.projects.filter(x=>x.name===p.name).length<2)return"";
  const parts=p.path.split("/");
  return ` <span class=mono style="color:var(--mut);font-weight:400">in ${esc(parts[parts.length-2])}</span>`;
}
function row(p){
  const running=["starting","serving","saving"].includes(p.phase);
  const saveBusy=p.phase==="starting"||p.saving;
  const path=esc(p.path);
  const stages=`<div class=slot>${p.stages.length?`<ol class=rail aria-label="Start progress">${p.stages.map(s=>
    `<li data-state=${s.state}${s.state==="active"?' aria-current="step"':""}><span class=bar><span class=fill></span></span>${esc(s.label)}</li>`).join("")}</ol>`:""}</div>`;
  const strip=ui.cancel.has(p.path)&&p.phase==="starting"?cancelStrip(p):(ui.confirm.has(p.path)&&(p.phase==="idle"||p.phase==="failed")?confirmStrip(p):"");
  const stoppable=p.phase==="serving"||(p.phase==="starting");
  const serveToggle=stoppable
    ?`<button class="btn quiet danger" data-act=${p.phase==="starting"&&p.cancelRisky?"stopAsk":"stop"} data-path="${path}" data-key="stop:${path}">${I.stop}${p.phase==="starting"?"Cancel":"Stop"}</button>`
    :`<button class="btn quiet" data-act=serve data-path="${path}" data-key="serve:${path}" ${running?"disabled":""} title="Run rojo serve only: no build, no publish, no Studio">${I.serve}Serve only</button>`;
  const startSub=p.phase==="starting"?"in progress":p.phase==="serving"?"running":"publish + serve";
  return `<article class=row data-path="${path}">
   <div class=head><div><h2 class=name>${esc(p.name)}${dupes(p)}</h2><div class=meta>${meta(p)}</div></div>
   <div class=hr><span class=state data-phase=${p.phase}><i class=dot></i>${stateLabel(p)}</span>
    <button class="icon${p.pinned?" on":""}" data-act=${p.pinned?"unpin":"pin"} data-path="${path}" data-key="pin:${path}" aria-pressed=${!!p.pinned} aria-label="Pin ${esc(p.name)} to the top" title="${p.pinned?"Unpin":"Pin to top"}">${I.pin}</button>
    <button class=icon data-act=hide data-path="${path}" data-key="hide:${path}" ${running?"disabled":""} aria-label="Hide ${esc(p.name)}" title="${running?"Stop it before hiding":"Hide (restore from Hidden below)"}">${I.hide}</button></div></div>
   <p class=say>${sentence(p)}</p>${stages}
   <div class=actions>${strip||`
    <div class=group role=group aria-label="Sync with Roblox">
     <button class="btn primary" data-act=start data-path="${path}" data-key="start:${path}" ${running?"disabled":""}><span class=t>${I.play}Start</span><span class=s>${startSub}</span></button>
     <button class=btn data-act=save data-path="${path}" data-key="save:${path}" ${saveBusy?"disabled":""}><span class=t>${I.save}Save</span><span class=s>${p.saving?"saving…":"Roblox → repo"}</span></button>
    </div>
    <div class=group role=group aria-label="Rojo server">${serveToggle}</div>`}
   </div>
   <details class=disclosure data-log="${path}" ${ui.logOpen.get(p.path)?"open":""}><summary>Log${p.log.length?` (${p.log.length})`:""}</summary>
    <pre>${esc(p.log.join("\n")||"Nothing yet.")}</pre></details>
  </article>`;
}
function studioText(s){
  if(!s)return"";
  const dot=c=>`<i class=dot style="background:${c}"></i>`;
  if(!s.running)return dot("var(--line-strong)")+"Studio closed";
  const w=s.windows;
  if(w===null)return dot("var(--ok)")+"Studio running";
  const unsaved=w.filter(x=>x.unsaved).length;
  return dot(unsaved?"var(--warn)":"var(--ok)")+"Studio running"+(unsaved?` · ${unsaved} with unsaved edits`:"");
}
function foldersPanel(){
  if(!ui.folders||!data)return"";
  const roots=data.configuredRoots,extra=data.roots.filter(r=>!roots.includes(r));
  return `<section class=panel aria-label="Folders"><div class=head><h2>Folders to scan</h2><button class=link data-act=closeFolders data-key=closeFolders>Close</button></div>
   <p class=hint>sgfl looks in these folders, up to three levels deep, for projects with an assets.json and default.project.json. Add the folder your games live in, then rescan.</p>
   <ul class=roots>${roots.map(r=>`<li><span class=mono>${esc(r)}</span><button class=link data-act=removeRoot data-path="${esc(r)}" data-key="rm:${esc(r)}">Remove</button></li>`).join("")||`<li class=mono>None yet</li>`}
   ${extra.map(r=>`<li><span class=mono>${esc(r)}</span><span class=hint style="margin:0">this run only</span></li>`).join("")}</ul>
   <div class=add><input id=rootInput data-key=rootInput placeholder="/Users/you/Documents/Code" aria-label="Folder path" spellcheck=false>
    <button class="btn quiet" data-act=addRoot data-key=addRoot>Add</button>
    ${data.canPick?`<button class="btn quiet" data-act=pick data-key=pick>Choose…</button>`:""}
    <button class="btn quiet" data-act=rescan data-key=rescan>Rescan</button></div>
   ${ui.rootError?`<p class=err role=alert>${esc(ui.rootError)}</p>`:""}
  </section>`;
}
function render(){
  if(!data)return;
  const active=document.activeElement&&document.activeElement.dataset?document.activeElement.dataset.key:null;
  const typed=$("#rootInput")?$("#rootInput").value:"";
  document.querySelectorAll("pre").forEach(p=>ui.scroll.set(p.closest("[data-path]").dataset.path,{top:p.scrollTop,pinned:p.scrollTop+p.clientHeight>=p.scrollHeight-6}));
  const hiddenHtml=data.hidden.length?`<details class="disclosure hidden-list" data-hidden ${ui.hiddenOpen?"open":""}><summary>Hidden (${data.hidden.length})</summary><ul class=roots>${data.hidden.map(h=>`<li><span>${esc(h.name)} <span class=mono style="color:var(--mut)">${esc(h.path)}</span></span><button class=link data-act=unhide data-path="${esc(h.path)}" data-key="show:${esc(h.path)}">Show</button></li>`).join("")}</ul></details>`:"";
  const html=(data.projects.length?`<div class=list>${data.projects.map(row).join("")}</div>`
   :`<div class="list empty"><b>${data.hidden.length?"Every project is hidden":"No sgfl projects found"}</b>${data.hidden.length?"Open Hidden below to bring one back.":"Open Folders and add the directory your games live in."}</div>`)+hiddenHtml;
  const fp=foldersPanel();
  if(html!==lastHtml){
    $("#app").innerHTML=html;lastHtml=html;
    document.querySelectorAll("pre").forEach(p=>{const m=ui.scroll.get(p.closest("[data-path]").dataset.path);p.scrollTop=m&&!m.pinned?m.top:p.scrollHeight});
  }
  if(fp!==lastFolders){$("#folders").innerHTML=fp;lastFolders=fp;if($("#rootInput"))$("#rootInput").value=typed}
  $("#studio").innerHTML=studioText(data.studio);
  $("[data-act=folders]").setAttribute("aria-expanded",ui.folders);
  if(active){const el=document.querySelector(`[data-key="${CSS.escape(active)}"]`);if(el&&el!==document.activeElement)el.focus()}
  tickClocks();
}
function tickClocks(){document.querySelectorAll("[data-since]").forEach(e=>{e.textContent=fmtClock(Date.now()/1000-parseFloat(e.dataset.since))})}
const announced={};
function announce(){
  for(const p of data.projects){
    const k=p.phase+((p.stages.find(s=>s.state==="active")||{}).key||"");
    if(announced[p.path]!==undefined&&announced[p.path]!==k)$("#live").textContent=`${p.name}: ${stateLabel(p)}`;
    announced[p.path]=k;
  }
}
async function poll(){try{data=await (await fetch("/api/status")).json();announce();render()}catch(e){}}
document.addEventListener("click",async e=>{
  const b=e.target.closest("[data-act]");if(!b||b.disabled)return;
  const path=b.dataset.path,a=b.dataset.act;
  if(a==="folders"||a==="closeFolders"){ui.folders=a==="folders"?!ui.folders:false;lastFolders=null;render();if(!ui.folders)$("[data-act=folders]").focus();return}
  if(a==="start"){
    const p=data&&data.projects.find(x=>x.path===path);
    if(!p)return;
    if(p.studioOpen!==false){ui.confirm.add(path);lastHtml="";render();return}
    return act(path,"start");
  }
  if(a==="startNow"){ui.confirm.delete(path);lastHtml="";return act(path,"start")}
  if(a==="cancel"){ui.confirm.delete(path);lastHtml="";render();return}
  if(a==="stopAsk"){ui.cancel.add(path);lastHtml="";render();return}
  if(a==="keep"){ui.cancel.delete(path);lastHtml="";render();return}
  if(a==="stopNow"){ui.cancel.delete(path);lastHtml="";return act(path,"stop")}
  if(["save","serve","stop","hide","pin","unpin"].includes(a))return act(path,a);
  if(a==="unhide"){await post("/api/hidden",{path,hidden:false});lastFolders=null;return poll()}
  if(a==="addRoot"){const r=await post("/api/roots",{add:$("#rootInput").value});ui.rootError=r.error||"";lastFolders=null;return poll()}
  if(a==="removeRoot"){await post("/api/roots",{remove:path});lastFolders=null;return poll()}
  if(a==="rescan"){await post("/api/rescan");return poll()}
  if(a==="pick"){const r=await post("/api/pick");if(r.path){const x=await post("/api/roots",{add:r.path});ui.rootError=x.error||""}lastFolders=null;return poll()}
});
document.addEventListener("keydown",e=>{if(e.key==="Escape"&&ui.folders){ui.folders=false;lastFolders=null;render();$("[data-act=folders]").focus()}
  if(e.key==="Enter"&&e.target.id==="rootInput")document.querySelector("[data-act=addRoot]").click()});
document.addEventListener("toggle",e=>{const d=e.target;if(d.dataset&&"hidden" in d.dataset)ui.hiddenOpen=d.open;if(d.dataset&&d.dataset.log)ui.logOpen.set(d.dataset.log,d.open)},true);
poll();setInterval(poll,1500);setInterval(tickClocks,1000);
</script></body></html>"""


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

def runDashboard(extraRoots: list, port: int) -> None:
    registry = Registry(extraRoots)
    token = secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The Host + token checks do not stop a hostile page framing this one and
            # steering a click onto Start; refuse to be framed at all.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode())

        def _hostOk(self) -> bool:
            # Defeats DNS rebinding: only answer to our own localhost origin.
            return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

        def _body(self) -> dict:
            try:
                length = min(max(int(self.headers.get("Content-Length") or 0), 0), 65536)
                body = json.loads(self.rfile.read(length) or b"{}")
                return body if isinstance(body, dict) else {}
            except (ValueError, OSError):
                return {}

        def do_GET(self):
            if not self._hostOk():
                return self._json({}, 403)
            if self.path == "/":
                return self._send(200, PAGE.replace("__TOKEN__", token).encode(), "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._json(registry.view())
            self._json({}, 404)

        def do_POST(self):
            sentToken = self.headers.get("X-Token") or ""
            if (
                not self._hostOk()
                or not secrets.compare_digest(sentToken.encode(), token.encode())
                or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
            ):
                return self._json({}, 403)
            body = self._body()
            if self.path == "/api/action":
                project = registry.get(str(body.get("path", "")))
                action = body.get("action")
                if project is None or registry.isHidden(project.path):
                    # a hidden project has no controls on screen; it must not be drivable blind
                    return self._json({"error": "Unknown project."}, 404)
                handlers = {
                    "start": project.startFull,
                    "serve": project.startServe,
                    "save": project.startSave,
                    "stop": project.stop,
                    "pin": lambda: registry.setPinned(project.path, True),
                    "unpin": lambda: registry.setPinned(project.path, False),
                }
                if action == "hide":
                    error = registry.setHidden(project.path, True)
                    return self._json({"error": error} if error else {}, 409 if error else 200)
                if action not in handlers:
                    return self._json({"error": "Unknown action."}, 400)
                handlers[action]()
                return self._json({})
            if self.path == "/api/roots":
                if "add" in body:
                    error = registry.addRoot(str(body["add"]))
                    return self._json({"error": error} if error else {})
                if "remove" in body:
                    registry.removeRoot(str(body["remove"]))
                    return self._json({})
            if self.path == "/api/hidden":
                error = registry.setHidden(str(body.get("path", "")), bool(body.get("hidden")))
                return self._json({"error": error} if error else {})
            if self.path == "/api/rescan":
                registry.rescan()
                return self._json({})
            if self.path == "/api/pick":
                return self._json({"path": pickFolder()})
            self._json({}, 404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as err:
        print(f"Could not start the dashboard on port {port}: {err}. Is another `sgfl ui` already running? Try --port.", file=sys.stderr)
        raise SystemExit(1)
    print(f"sgfl dashboard: http://127.0.0.1:{port}  (Ctrl+C stops it and every process it started)", flush=True)
    for p in registry.projects.values():
        print(f"  - {p.path}", flush=True)
    if not registry.projects:
        print("  (no projects found; add a folder from the Folders button)", flush=True)

    # SIGTERM (pkill, a closing terminal) must stop the children too: they run in
    # their own sessions, so without this they are orphaned and keep their ports.
    def _terminate(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for p in list(registry.projects.values()):
            p.stop()
