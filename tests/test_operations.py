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
