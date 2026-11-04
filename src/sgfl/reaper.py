"""Stops `sgfl ui`'s children when the dashboard dies, however it dies.

The dashboard starts each `sgfl start` / `rojo serve` as the root of its own
process tree (proctree.py) so Stop reaches sgfl and the rojo under it. The cost
is that nothing else reaches them: a dashboard that is SIGKILLed, crashes, or loses its terminal
leaves them running and holding their ports, and the next dashboard then moves
off the port Studio's Rojo plugin is pointed at.

This process is started by the dashboard with a pipe on stdin, over which it
hears "+<pid>" when a tree starts and "-<pid>" when it ends. The kernel
closes that pipe whichever way the dashboard exits, so EOF is the one signal
that cannot be missed, on Windows as well. Stdlib only (plus proctree), so it
starts fast and has nothing to break.
"""

import sys
import time

from . import proctree

# Interrupt first so `sgfl start` runs its `finally` (Place.rbxl cleanup), then harder.
# (On Windows the reaper has no console, so the interrupt fails and terminate follows at once.)
ESCALATION = ((proctree.interrupt, 5), (proctree.terminate, 5), (proctree.kill, 0))


def reap(roots: set) -> None:
    for stop, wait in ESCALATION:
        roots = {r for r in roots if proctree.alive(r)}
        if not roots:
            return
        delivered = [stop(r) is not False for r in roots]
        if not any(delivered):
            continue  # nothing was told to stop, so there is nothing to wait for
        deadline = time.time() + wait
        while time.time() < deadline and any(proctree.alive(r) for r in roots):
            time.sleep(0.2)


def main() -> None:
    roots: set = set()
    for line in sys.stdin:
        line = line.strip()
        try:
            pid = int(line[1:])
        except ValueError:
            continue
        if line.startswith("+"):
            roots.add(pid)
        elif line.startswith("-"):
            roots.discard(pid)
    reap(roots)


if __name__ == "__main__":
    main()
