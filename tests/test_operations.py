import json

import pytest

from sgfl import operations
from sgfl.util import SGFLError


def test_load_asset_table_reads_the_project_config(isolatedCwd):
    (isolatedCwd / "assets.json").write_text(
        json.dumps({"Lighting": {"folder": "map", "robloxPath": "Lighting"}})
    )

    assetTable = operations._loadAssetTable()

    assert set(assetTable.keys()) == {"Lighting"}
    assert assetTable["Lighting"]["mode"] == "file"


def test_load_asset_table_refuses_to_fall_back_to_the_packaged_default(isolatedCwd):
    """Running from the wrong directory must fail, not silently adopt the
    default table — that would have save write entry files into invented
    folders and publish apply a config this project never declared."""
    with pytest.raises(SGFLError) as excinfo:
        operations._loadAssetTable()

    assert "assets.json" in excinfo.value.message


def test_resolve_rojo_serve_port_uses_an_explicit_port_verbatim():
    """--port means exactly that port — no scanning, even if it is taken, so
    a conflict fails loudly in rojo instead of landing somewhere unexpected."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        takenPort = sock.getsockname()[1]

        assert operations._resolveRojoServePort(takenPort) == takenPort


def test_resolve_rojo_serve_port_returns_none_when_the_default_is_free(
    isolatedCwd, monkeypatch
):
    """A free configured port must resolve to None so the plain `rojo serve`
    invocation stays identical to previous versions."""
    monkeypatch.setattr(operations, "_configuredRojoPort", lambda: 34872)
    monkeypatch.setattr(operations, "_portIsFree", lambda port: True)

    assert operations._resolveRojoServePort(None) is None


def test_resolve_rojo_serve_port_scans_upward_when_the_default_is_taken(
    isolatedCwd, monkeypatch, capsys
):
    monkeypatch.setattr(operations, "_configuredRojoPort", lambda: 34872)
    monkeypatch.setattr(operations, "_portIsFree", lambda port: port >= 34874)

    resolved = operations._resolveRojoServePort(None)

    assert resolved == 34874
    # The bump must be loud: the Rojo plugin in Studio defaults to the
    # standard port, so the user has to be told where the server went.
    assert "34874" in capsys.readouterr().out


def test_resolve_rojo_serve_port_fails_loud_when_the_scan_is_exhausted(
    isolatedCwd, monkeypatch
):
    monkeypatch.setattr(operations, "_configuredRojoPort", lambda: 34872)
    monkeypatch.setattr(operations, "_portIsFree", lambda port: False)

    with pytest.raises(SGFLError) as excinfo:
        operations._resolveRojoServePort(None)

    assert "--port" in " ".join(excinfo.value.suggestions)


def test_configured_rojo_port_reads_serve_port_from_the_project_file(isolatedCwd):
    (isolatedCwd / "default.project.json").write_text(
        json.dumps({"name": "Game", "servePort": 40000, "tree": {}})
    )

    assert operations._configuredRojoPort() == 40000


def test_configured_rojo_port_defaults_without_a_project_file(isolatedCwd):
    assert operations._configuredRojoPort() == operations.DEFAULT_ROJO_PORT


# --- wally dependency sync ------------------------------------------------


def _stubWallyTools(monkeypatch, *, wally="/stub/wally", packageTypes=None):
    tools = {"wally": wally, "wally-package-types": packageTypes}
    monkeypatch.setattr(operations.shutil, "which", lambda name: tools.get(name))
    monkeypatch.delenv("SGFL_CI", raising=False)


def _stubRunCommand(monkeypatch, isolatedCwd, failOn=None):
    """Record commands; `wally install` creates Packages/ like the real one."""
    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        if failOn and command[0] == failOn:
            raise SGFLError(f"{failOn} failed")
        if command[:2] == ["wally", "install"]:
            (isolatedCwd / "Packages").mkdir(exist_ok=True)

    monkeypatch.setattr(operations, "runCommand", fake)
    return calls


def _writeWallyProject(isolatedCwd):
    (isolatedCwd / "wally.toml").write_text('[dependencies]\nreact = "jsdotlua/react@17.2.1"\n')
    (isolatedCwd / "wally.lock").write_text('name = "jsdotlua/react"\nversion = "17.2.1"\n')


def test_sync_wally_is_a_noop_without_a_manifest(isolatedCwd, monkeypatch):
    calls = _stubRunCommand(monkeypatch, isolatedCwd)

    operations._syncWallyPackages()

    assert calls == []


def test_sync_wally_installs_on_a_fresh_clone_and_stamps(isolatedCwd, monkeypatch):
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)

    operations._syncWallyPackages()

    assert ["wally", "install"] in calls
    assert (isolatedCwd / "Packages" / operations.WALLY_STAMP_NAME).exists()


def test_sync_wally_skips_when_packages_are_fresh(isolatedCwd, monkeypatch):
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)
    operations._syncWallyPackages()
    calls.clear()

    operations._syncWallyPackages()

    assert calls == []


def test_sync_wally_reinstalls_when_the_manifest_changes(isolatedCwd, monkeypatch):
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)
    operations._syncWallyPackages()
    calls.clear()

    (isolatedCwd / "wally.toml").write_text('[dependencies]\nripple = "littensy/ripple@0.10.2"\n')
    operations._syncWallyPackages()

    assert ["wally", "install"] in calls


def test_sync_wally_reinstalls_when_packages_were_deleted(isolatedCwd, monkeypatch):
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)
    operations._syncWallyPackages()
    calls.clear()

    import shutil as realShutil

    realShutil.rmtree(isolatedCwd / "Packages")
    operations._syncWallyPackages()

    assert ["wally", "install"] in calls


def test_sync_wally_uses_locked_install_in_ci(isolatedCwd, monkeypatch):
    """CI must fail loudly on a stale lockfile, never silently re-resolve to
    versions nobody tested against."""
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)
    monkeypatch.setenv("SGFL_CI", "1")

    operations._syncWallyPackages()

    assert ["wally", "install", "--locked"] in calls


def test_sync_wally_bootstraps_the_toolchain_when_wally_is_missing(isolatedCwd, monkeypatch):
    """Fresh machine + fresh clone: rokit.toml provisions wally, so a missing
    wally binary means `rokit install` first — clone to start with zero
    manual steps."""
    _writeWallyProject(isolatedCwd)
    (isolatedCwd / "rokit.toml").write_text("[tools]\n")
    _stubWallyTools(monkeypatch, wally=None)
    calls = _stubRunCommand(monkeypatch, isolatedCwd)

    operations._syncWallyPackages()

    assert calls[0] == ["rokit", "install"]
    assert ["wally", "install"] in calls


def test_sync_wally_runs_the_type_fixup_when_available(isolatedCwd, monkeypatch):
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch, packageTypes="/stub/wally-package-types")
    calls = _stubRunCommand(monkeypatch, isolatedCwd)

    operations._syncWallyPackages()

    assert ["rojo", "sourcemap", "default.project.json", "--output", "sourcemap.json"] in calls
    assert ["wally-package-types", "--sourcemap", "sourcemap.json", "Packages"] in calls


def test_sync_wally_type_fixup_failure_warns_but_keeps_the_install(isolatedCwd, monkeypatch, capsys):
    """The build itself is correct once packages are installed — a fixup
    failure must not abort sgfl start, and must not force a reinstall on the
    next run."""
    _writeWallyProject(isolatedCwd)
    _stubWallyTools(monkeypatch, packageTypes="/stub/wally-package-types")
    calls = _stubRunCommand(monkeypatch, isolatedCwd, failOn="rojo")

    operations._syncWallyPackages()

    assert "Package type fixup failed" in capsys.readouterr().out
    assert (isolatedCwd / "Packages" / operations.WALLY_STAMP_NAME).exists()
    calls.clear()
    operations._syncWallyPackages()
    assert calls == []
