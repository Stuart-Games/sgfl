"""Real processes, on whatever OS runs the suite (CI runs it on Windows too).

These are the guarantees `sgfl ui` leans on: stopping a run reaches the rojo
*under* `sgfl start`, the polite stop lets `finally` run, and the reaper cleans
up after a dashboard that died without saying so.
"""

import os
import subprocess
import sys
import time

import pytest

from sgfl import proctree

# A child that starts a grandchild, reports the grandchild's pid, then waits.
TREE = r"""
import subprocess, sys, time
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
print(grandchild.pid, flush=True)
time.sleep(120)
"""

# A child that reports when its interrupt handling is in place, then writes a marker
# from `finally` — what sgfl's own cleanup depends on.
GRACEFUL = r"""
import signal, sys, time
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)  # what sgfl/cli.py installs
try:
    print("ready", flush=True)
    time.sleep(120)
finally:
    open(sys.argv[1], "w").write("cleaned up")
"""


def _pidAlive(pid: int) -> bool:
    if proctree.WINDOWS:
        return proctree._windowsAlive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers; ps tells them apart.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith("Z")


def _waitDead(pid: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pidAlive(pid):
            return True
        time.sleep(0.2)
    return False


def _spawnTree():
    child = subprocess.Popen(
        [sys.executable, "-c", TREE], stdout=subprocess.PIPE, text=True, **proctree.popenKwargs()
    )
    grandchild = int(child.stdout.readline())
    return child, grandchild


def test_terminate_reaches_the_grandchild():
    child, grandchild = _spawnTree()
    try:
        assert proctree.alive(child.pid)
        proctree.terminate(child.pid)
        child.wait(timeout=15)
        assert _waitDead(grandchild), "the grandchild (rojo under sgfl start) survived"
        assert _waitDead(child.pid)
    finally:
        proctree.kill(child.pid)


def test_kill_reaches_the_grandchild():
    child, grandchild = _spawnTree()
    try:
        proctree.kill(child.pid)
        child.wait(timeout=15)
        assert _waitDead(grandchild)
    finally:
        proctree.kill(child.pid)


def test_interrupt_lets_finally_run(tmp_path):
    marker = tmp_path / "marker"
    child = subprocess.Popen(
        [sys.executable, "-c", GRACEFUL, str(marker)], stdout=subprocess.PIPE, text=True, **proctree.popenKwargs()
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        if not proctree.interrupt(child.pid):
            pytest.skip("no console to deliver Ctrl+Break to (Windows without a console)")
        child.wait(timeout=15)
        assert marker.read_text() == "cleaned up"
    finally:
        proctree.kill(child.pid)


def test_alive_is_false_once_the_tree_is_gone():
    child = subprocess.Popen([sys.executable, "-c", "pass"], **proctree.popenKwargs())
    child.wait(timeout=15)
    deadline = time.time() + 5
    while proctree.alive(child.pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not proctree.alive(child.pid)


def test_reaper_kills_a_tree_when_its_pipe_closes():
    child, grandchild = _spawnTree()
    reaper = subprocess.Popen(
        [sys.executable, "-m", "sgfl.reaper"], stdin=subprocess.PIPE, text=True, **proctree.detachedKwargs()
    )
    try:
        reaper.stdin.write(f"+{child.pid}\n")
        reaper.stdin.close()  # what the OS does when the dashboard dies, any way it dies
        reaper.wait(timeout=30)
        assert _waitDead(grandchild) and _waitDead(child.pid)
    finally:
        proctree.kill(child.pid)


def test_reaper_leaves_untracked_trees_alone():
    child, grandchild = _spawnTree()
    reaper = subprocess.Popen(
        [sys.executable, "-m", "sgfl.reaper"], stdin=subprocess.PIPE, text=True, **proctree.detachedKwargs()
    )
    try:
        reaper.stdin.write(f"+{child.pid}\n-{child.pid}\n")  # finished normally
        reaper.stdin.close()
        reaper.wait(timeout=30)
        assert _pidAlive(child.pid) and _pidAlive(grandchild)
    finally:
        proctree.kill(child.pid)
        child.wait(timeout=15)


@pytest.mark.skipif(not proctree.WINDOWS, reason="Ctrl+Break exists only on Windows")
def test_cli_turns_ctrl_break_into_keyboard_interrupt():
    import signal

    import sgfl.cli  # noqa: F401  (installs the handler on import)

    assert signal.getsignal(signal.SIGBREAK) is signal.default_int_handler
