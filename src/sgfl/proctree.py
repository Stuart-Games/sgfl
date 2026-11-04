"""Start, stop and probe a process *and everything under it*, on macOS/Linux and Windows.

`sgfl ui` runs `sgfl start` (which itself runs `rojo serve`) and stops them as one
unit, so the unit is the process tree, not the pid. The two platforms get there
differently:

- POSIX: the child leads its own session, so its pid is a process-group id and
  one killpg reaches sgfl and the rojo under it. SIGINT first (sgfl's `finally`
  blocks run), then SIGTERM, then SIGKILL.
- Windows: there are no process groups to signal, only console groups. The child
  gets its own (CREATE_NEW_PROCESS_GROUP), which is what Ctrl+Break can be sent
  to; Ctrl+C cannot reach a separate group at all. sgfl maps Ctrl+Break to
  KeyboardInterrupt (cli.py), so the polite step still runs its `finally`. The
  hard steps are `taskkill /T /F`, which walks live descendants. Python's
  os.kill(pid, n) for any other n is TerminateProcess on the pid alone, which
  would orphan rojo, so it is never used here.
"""

import os
import signal
import subprocess
import sys

WINDOWS = sys.platform == "win32"
_STILL_ACTIVE = 259


def popenKwargs() -> dict:
    """Popen arguments that make the child the root of a tree we can stop as one."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detachedKwargs() -> dict:
    """For the reaper: it must outlive our terminal (a hangup, a closed console window)."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def interrupt(pid: int) -> bool:
    """The polite stop. False if it could not be delivered (no shared console on
    Windows, already gone); the caller should then go straight to terminate."""
    try:
        if WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(pid, signal.SIGINT)
        return True
    except OSError:
        return False


def terminate(pid: int) -> None:
    if WINDOWS:
        _taskkill(pid)
    else:
        _killpg(pid, signal.SIGTERM)


def kill(pid: int) -> None:
    if WINDOWS:
        _taskkill(pid)  # /F already is the hard stop; a repeat catches late spawns
    else:
        _killpg(pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    """Is anything of this tree still running? (POSIX: any group member; Windows: the root.)"""
    if WINDOWS:
        return _windowsAlive(pid)
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _killpg(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except OSError:
        pass


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def _windowsAlive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists, it just is not ours
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)
