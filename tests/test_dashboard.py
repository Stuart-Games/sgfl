import os

from sgfl import dashboard
from sgfl.dashboard import Project, Registry, discover


def _makeProject(path):
    os.makedirs(path)
    for name in ("assets.json", "default.project.json"):
        with open(os.path.join(path, name), "w") as f:
            f.write("{}")


def test_discover_finds_projects_and_prunes(tmp_path):
    _makeProject(str(tmp_path / "games" / "alpha"))
    _makeProject(str(tmp_path / "beta"))
    _makeProject(str(tmp_path / "node_modules" / "ghost"))  # pruned
    _makeProject(str(tmp_path / ".hidden" / "ghost2"))  # pruned
    _makeProject(str(tmp_path / "a" / "b" / "c" / "d" / "toodeep"))  # past depth 3
    _makeProject(str(tmp_path / "alpha-app" / "nested"))  # inside a project: never descended
    found = [os.path.relpath(p, tmp_path) for p in discover([str(tmp_path)])]
    assert set(found) == {os.path.join("games", "alpha"), "beta", os.path.join("alpha-app", "nested")}
    assert not any("ghost" in p or "toodeep" in p for p in found)


def test_discover_does_not_descend_into_a_project(tmp_path):
    _makeProject(str(tmp_path / "outer"))
    _makeProject(str(tmp_path / "outer" / "inner"))
    assert [os.path.basename(p) for p in discover([str(tmp_path)])] == ["outer"]


def test_stage_markers_follow_real_start_output(tmp_path):
    project = Project(str(tmp_path))
    project.kind, project.stage, project.runId = "start", 0, 1
    expected = [
        ("INFO Checking environment configuration for publish flow.", 0, False),
        ("INFO Compiling scripts via Rojo into Place.rbxl.", 0, False),
        ("INFO Uploading script build as a Saved base version.", 1, False),
        ("INFO Running task (in-engine, universe 1, place 2)", 1, False),
        ("INFO Downloading applied place (version 5) for sidecar patch.", 2, False),
        ("INFO Opening Roblox Studio for the published place.", 3, False),
        ("INFO Starting Rojo server on port 34873.", 4, False),
        ("Rojo server listening:", 5, True),
    ]
    for line, stage, listening in expected:
        project._observe(1, line)
        assert (project.stage, project.listening) == (stage, listening), line


def test_stale_run_output_is_ignored(tmp_path):
    project = Project(str(tmp_path))
    project.kind, project.stage, project.runId = "start", 0, 2
    project._observe(1, "INFO Starting Rojo server on port 1.")  # a previous run's late line
    assert project.stage == 0 and not project.log


def test_published_version_is_recorded_on_start(tmp_path, monkeypatch):
    recorded = {}
    monkeypatch.setattr(dashboard, "_recordVersion", lambda path, version: recorded.update({path: version}))
    project = Project(str(tmp_path))
    project.kind, project.runId = "start", 1
    project._observe(1, "INFO Published place version 42.")
    assert recorded == {project.path: 42}


def test_hiding_a_running_project_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "CONFIG_PATH", str(tmp_path / "ui.json"))
    monkeypatch.setattr(dashboard, "CREDENTIALS_DIR", str(tmp_path))
    registry = Registry([])
    project = Project(str(tmp_path / "p"))
    project.done = False
    registry.projects = {project.path: project}
    assert registry.setHidden(project.path, True) == "Stop this project before hiding it."
    project.done = True
    assert registry.setHidden(project.path, True) is None


def test_ports_are_not_handed_out_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_portFree", lambda port: True)
    dashboard._claimedPorts.clear()
    a, b = Project(str(tmp_path / "a")), Project(str(tmp_path / "b"))
    first, second = a._claimPort(), b._claimPort()
    assert first != second
    a._releasePort()
    b._releasePort()
    assert not dashboard._claimedPorts


def test_save_can_run_while_serving_but_not_while_starting(tmp_path, monkeypatch):
    launched = []

    class FakeProc:
        pid = 1
        stdout = iter(())

        def wait(self):
            return 0

    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **k: launched.append(a[0]) or FakeProc())
    project = Project(str(tmp_path))

    project.done, project.kind, project.listening = False, "start", False  # still building
    project.startSave()
    assert not launched

    project.listening = True  # now serving
    project.startSave()
    assert launched and launched[0][-1] == "save"


def test_hidden_projects_are_reported_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "CONFIG_PATH", str(tmp_path / "ui.json"))
    monkeypatch.setattr(dashboard, "CREDENTIALS_DIR", str(tmp_path))
    registry = Registry([])
    project = Project(str(tmp_path / "p"))
    registry.projects = {project.path: project}
    assert not registry.isHidden(project.path)
    registry.setHidden(project.path, True)
    assert registry.isHidden(project.path)


class _FakeProc:
    pid = 1
    stdout = iter(())

    def wait(self):
        return 0


def _serving(tmp_path, monkeypatch, launched):
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **k: launched.append(a[0]) or _FakeProc())
    monkeypatch.setattr(dashboard.threading, "Thread", lambda target, args=(), daemon=False: type("T", (), {"start": lambda s: None, "join": lambda s, timeout=None: None})())
    monkeypatch.setattr(dashboard, "_portFree", lambda port: True)
    dashboard._claimedPorts.clear()
    project = Project(str(tmp_path))
    project.startServe()
    project.listening = True
    return project


def test_a_serve_that_dies_comes_back_on_its_port(tmp_path, monkeypatch):
    launched = []
    project = _serving(tmp_path, monkeypatch, launched)
    port = project.port
    project._finish(project.runId, 1)
    assert project.restartPending and project.running() and project.port == port
    project._restart(project.runId, port, 0)
    assert launched[-1] == ["rojo", "serve", "--port", str(port)]
    assert project.restarted and not project.restartPending and project.running()


def test_a_crash_looping_serve_gives_up(tmp_path, monkeypatch):
    launched = []
    project = _serving(tmp_path, monkeypatch, launched)
    for _ in dashboard.RESTART_DELAYS:
        project._finish(project.runId, 1)
        project._restart(project.runId, project.port, 0)  # dies again before ever listening
    project._finish(project.runId, 1)
    assert not project.running() and not project.restartPending
    assert project.last["ok"] is False and "gave up" in project.last["text"]
    assert not dashboard._claimedPorts


def test_stop_and_build_failures_are_not_restarted(tmp_path, monkeypatch):
    launched = []
    project = _serving(tmp_path, monkeypatch, launched)
    for name in ("interrupt", "terminate", "kill"):  # FakeProc's pid is 1: never signal it for real
        monkeypatch.setattr(dashboard.proctree, name, lambda pid: True)
    project.stop()
    project._finish(project.runId, 1)
    assert not project.running() and project.last["text"] == "Stopped"

    project.startFull()  # a start that dies while building never served: no retry
    project._finish(project.runId, 1)
    assert not project.running() and not project.restartPending


def test_stop_calls_off_a_pending_restart(tmp_path, monkeypatch):
    launched = []
    project = _serving(tmp_path, monkeypatch, launched)
    project._finish(project.runId, 1)
    pending = project.runId
    project.stop()
    assert not project.running() and project.last["text"] == "Stopped"
    count = len(launched)
    project._restart(pending, 34872, 0)
    assert len(launched) == count


def test_studio_wait_is_tracked_until_studio_opens(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "_recordVersion", lambda path, version: None)
    monkeypatch.setattr(dashboard.time, "time", lambda: 1000.0)
    project = Project(str(tmp_path))
    project.kind, project.stage, project.runId, project.done = "start", 0, 1, False
    project._observe(1, "INFO Published place version 12.")
    project._observe(1, "INFO Another Studio launched 5s ago; waiting 20s so it finishes signing in first.")
    assert project.studioWaitUntil == 1020.0
    project._observe(1, "INFO Opening Roblox Studio for the published place.")
    assert project.studioWaitUntil == 0.0


def test_children_get_utf8_pipes_and_their_own_tree(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(dashboard.subprocess, "Popen", lambda *a, **k: seen.update(k) or _FakeProc())
    Project(str(tmp_path))._popen(["rojo", "serve"], reap=False)
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["env"]["PYTHONUTF8"] == "1"
    for key, value in dashboard.proctree.popenKwargs().items():
        assert seen[key] == value


def test_studio_running_reads_tasklist_on_windows(monkeypatch):
    import sys

    class Out:
        returncode = 0
        stdout = ""

    def fakeRun(args, **kwargs):
        assert args[0] == "tasklist"
        return Out()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dashboard.subprocess, "run", fakeRun)
    Out.stdout = "INFO: No tasks are running which match the specified criteria.\r\n"
    assert dashboard.studioRunning() is False
    Out.stdout = '"RobloxStudioBeta.exe","1234","Console","1","812,000 K"\r\n'
    assert dashboard.studioRunning() is True


def test_studio_windows_reads_powershell_titles_on_windows(monkeypatch):
    import sys

    class Out:
        returncode = 0
        stdout = "WaterPhysics - Roblox Studio\r\nVesselBuilder* - Roblox Studio\r\n\r\n"

    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dashboard, "studioRunning", lambda: True)
    monkeypatch.setattr(dashboard, "_windowCache", (0.0, None))
    monkeypatch.setattr(dashboard.subprocess, "run", lambda args, **k: calls.append(args) or Out())
    windows = dashboard.studioWindows()
    assert calls[0][0] == "powershell"
    assert windows == [
        {"title": "WaterPhysics - Roblox Studio", "unsaved": False},
        {"title": "VesselBuilder* - Roblox Studio", "unsaved": True},
    ]


def test_folder_picker_on_windows_keeps_a_drive_root(monkeypatch):
    import sys

    class Out:
        returncode = 0
        stdout = "C:\\\r\n"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(dashboard.subprocess, "run", lambda args, **k: Out())
    assert dashboard.pickFolder() == "C:\\"
    Out.returncode, Out.stdout = 1, ""  # cancelled
    assert dashboard.pickFolder() is None
