import json
import os

import pytest

from sgfl import cloud
from sgfl.util import SGFLError


def _writeAssetFile(root, relPath, content, newline="\n"):
    path = root / relPath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8").replace(b"\n", newline.encode()))
    return path


def test_collect_entry_files_reads_text_and_blob_tiers(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {
            "Lighting": {"folder": "src/Lighting", "robloxPath": "Lighting", "format": "text"},
            "Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace", "format": "blob"},
        }
    )
    _writeAssetFile(isolatedCwd, "src/Lighting/Lighting.sgfl", "[.] Lighting\nBrightness = 2\n")
    _writeAssetFile(isolatedCwd, "src/Workspace/Workspace.sgfl", "[.] Workspace\n")
    _writeAssetFile(isolatedCwd, "src/Workspace/Workspace.sgfl.rbxm", b"\x00binary\x00")

    files = dict(cloud.collectEntryFiles(assetTable))
    assert files["src/Lighting/Lighting.sgfl"] == b"[.] Lighting\nBrightness = 2\n"
    assert files["src/Workspace/Workspace.sgfl.rbxm"] == b"\x00binary\x00"


def test_collect_entry_files_missing_file_is_not_an_error(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Lighting": {"folder": "src/Lighting", "robloxPath": "Lighting"}}
    )
    assert cloud.collectEntryFiles(assetTable) == []


def test_collect_entry_files_children_mode_reads_managed_children(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "Modules": {"folder": "src/Modules", "robloxPath": "ServerScriptService.Modules", "mode": "children"},
        }
    )
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildB.sgfl.rbxm", b"blob-bytes")
    _writeAssetFile(isolatedCwd, "src/Modules/README.md", "not managed, must be ignored")

    files = dict(cloud.collectEntryFiles(assetTable))
    assert set(files.keys()) == {
        "src/Modules/Modules.sgfl",
        "src/Modules/ChildA.sgfl",
        "src/Modules/ChildB.sgfl.rbxm",
    }


def test_collect_entry_files_rejects_crlf_structural_lines(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Lighting": {"folder": "src/Lighting", "robloxPath": "Lighting"}}
    )
    _writeAssetFile(isolatedCwd, "src/Lighting/Lighting.sgfl", "[.] Lighting\r\nBrightness = 2\r\n", newline="")

    with pytest.raises(SGFLError, match="CRLF"):
        cloud.collectEntryFiles(assetTable)


def test_parse_sidecar_blocks_round_trips_rendered_value(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace"}}
    )
    _writeAssetFile(
        isolatedCwd,
        "src/Workspace/Workspace.sgfl",
        "[.] Workspace\nStreamingEnabled = true\n\n[!sidecar .]\nCollisionGroupData = Base64(\"AQID\")\n",
    )

    patches = cloud.parseSidecarBlocks(assetTable)
    assert patches[("Workspace", "CollisionGroupData")] == [b"\x01\x02\x03"]


def test_parse_sidecar_blocks_supports_non_dot_target(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace"}}
    )
    _writeAssetFile(
        isolatedCwd,
        "src/Workspace/Workspace.sgfl",
        "[.] Workspace\n\n[!sidecar Lighting]\nTechnology = 2\n",
    )
    patches = cloud.parseSidecarBlocks(assetTable)
    assert patches[("Lighting", "Technology")] == [2]


def _staleTestConfig():
    return cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "SharedGui": {
                "folder": "sglib/Gui/Shared",
                "robloxPath": "StarterGui.Shared",
                "mode": "children",
            },
            "Lighting": {"folder": "map", "robloxPath": "Lighting"},
            "Workspace": {"folder": "map", "robloxPath": "Workspace"},
        }
    )


def test_find_stale_entry_files_flags_moved_folder_and_removed_entry(isolatedCwd):
    assetTable = _staleTestConfig()
    # current, expected files
    _writeAssetFile(isolatedCwd, "sglib/Gui/Shared/SharedGui.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "sglib/Gui/Shared/Tools.sgfl", "[.] ScreenGui\n")
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n")
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl", "[.] Workspace\n")
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl.rbxm", b"\x00")  # Workspace defaults to blob
    # leftovers: SharedGui's folder used to be sglib/Gui, and an entry was deleted
    _writeAssetFile(isolatedCwd, "sglib/Gui/SharedGui.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "sglib/Gui/Tools.sgfl", "[.] ScreenGui\n")
    _writeAssetFile(isolatedCwd, "assets/OldEntry.sgfl.rbxm", b"\x00")

    assert cloud.findStaleEntryFiles(assetTable) == [
        "assets/OldEntry.sgfl.rbxm",
        "sglib/Gui/SharedGui.sgfl",
        "sglib/Gui/Tools.sgfl",
    ]


def test_find_stale_entry_files_clean_project_finds_nothing(isolatedCwd):
    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, "sglib/Gui/Shared/SharedGui.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "sglib/Gui/Shared/Tools.sgfl.rbxm", b"\x00")
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n")
    _writeAssetFile(isolatedCwd, "map/README.md", "never touched")

    assert cloud.findStaleEntryFiles(assetTable) == []


def test_find_stale_entry_files_ignores_git_dir(isolatedCwd):
    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, ".git/objects/whatever.sgfl", "[.] Folder\n")

    assert cloud.findStaleEntryFiles(assetTable) == []


def test_find_stale_entry_files_flags_rbxm_left_by_blob_to_text_switch(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Lighting": {"folder": "map", "robloxPath": "Lighting", "format": "text"}}
    )
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n")
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl.rbxm", b"\x00")  # from when it was blob

    assert cloud.findStaleEntryFiles(assetTable) == ["map/Lighting.sgfl.rbxm"]


class _FakeTtyStdin:
    def isatty(self):
        return True


def test_sweep_stale_deletes_only_after_yes(isolatedCwd, monkeypatch):
    import sys

    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, "sglib/Gui/SharedGui.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "sglib/Gui/Shared/SharedGui.sgfl", "[.] Folder\n")
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())
    monkeypatch.setattr(cloud, "confirmToggle", lambda *args, **kwargs: True)

    cloud.sweepStaleEntryFiles(assetTable)

    assert not (isolatedCwd / "sglib/Gui/SharedGui.sgfl").exists()
    assert (isolatedCwd / "sglib/Gui/Shared/SharedGui.sgfl").exists()


def test_sweep_stale_keeps_files_on_no(isolatedCwd, monkeypatch):
    import sys

    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, "sglib/Gui/SharedGui.sgfl", "[.] Folder\n")
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())
    monkeypatch.setattr(cloud, "confirmToggle", lambda *args, **kwargs: False)

    cloud.sweepStaleEntryFiles(assetTable)

    assert (isolatedCwd / "sglib/Gui/SharedGui.sgfl").exists()


def test_sweep_stale_non_tty_lists_and_keeps(isolatedCwd, capsys):
    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, "sglib/Gui/SharedGui.sgfl", "[.] Folder\n")

    cloud.sweepStaleEntryFiles(assetTable)  # pytest's stdin is not a TTY

    assert (isolatedCwd / "sglib/Gui/SharedGui.sgfl").exists()
    out = capsys.readouterr().out
    assert "sglib/Gui/SharedGui.sgfl" in out
    assert "Non-interactive" in out


def test_sweep_stale_noop_on_clean_project(isolatedCwd, capsys):
    assetTable = _staleTestConfig()
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n")

    cloud.sweepStaleEntryFiles(assetTable)  # must not print or prompt

    assert capsys.readouterr().out == ""


def test_children_sweep_removes_stale_child(isolatedCwd, capsys):
    entries = [{"name": "Modules", "folder": "src/Modules", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")
    _writeAssetFile(isolatedCwd, "src/Modules/Renamed.sgfl", "[.] ModuleScript\n")
    written = {"src/Modules/Modules.sgfl": b"", "src/Modules/ChildA.sgfl": b""}

    cloud._sweepChildrenFolders(entries, written)

    assert (isolatedCwd / "src/Modules/ChildA.sgfl").exists()
    assert not (isolatedCwd / "src/Modules/Renamed.sgfl").exists()


def test_children_sweep_skips_entry_that_failed_to_project(isolatedCwd):
    entries = [{"name": "Modules", "folder": "src/Modules", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")

    # entry produced nothing this save (projection warning path)
    cloud._sweepChildrenFolders(entries, {})

    assert (isolatedCwd / "src/Modules/Modules.sgfl").exists()
    assert (isolatedCwd / "src/Modules/ChildA.sgfl").exists()


def test_build_projection_config_includes_rojo_paths_and_sidecar_props(isolatedCwd):
    (isolatedCwd / "default.project.json").write_text(
        json.dumps({"tree": {"ReplicatedFirst": {"$path": "src/ReplicatedFirst"}}})
    )
    assetTable = cloud.normalizeAssetConfig(
        {"Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace"}}
    )
    config = cloud.buildProjectionConfig(assetTable)
    assert config["rojoPaths"] == ["ReplicatedFirst"]
    assert config["entries"][0]["name"] == "Workspace"
    assert "Workspace.CollisionGroupData" in config["sidecarProps"]
    assert "Gravity" in config["anchorProps"]


def test_build_projection_config_no_project_json(isolatedCwd):
    assetTable = cloud.normalizeAssetConfig(
        {"Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace"}}
    )
    config = cloud.buildProjectionConfig(assetTable)
    assert config["rojoPaths"] == []
