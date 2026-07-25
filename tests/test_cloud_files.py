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


class _FakeTtyStdin:
    def isatty(self):
        return True


def test_entry_files_outside_declared_paths_are_inert(isolatedCwd):
    """No project-wide stale sweep exists (and none is needed): files sgfl does
    not own — a vendored library, a submodule, a sibling game — are never read
    for publish, so they cannot affect the place."""
    assert not hasattr(cloud, "sweepStaleEntryFiles")
    assert not hasattr(cloud, "findStaleEntryFiles")

    assetTable = cloud.normalizeAssetConfig(
        {"Lighting": {"folder": "map", "robloxPath": "Lighting", "format": "text"}}
    )
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n")
    # a library the project vendors, plus a leftover from a renamed entry
    _writeAssetFile(isolatedCwd, "sglib/Gui/SharedGui.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "map/OldName.sgfl", "[.] Lighting\n")

    assert [name for name, _ in cloud.collectEntryFiles(assetTable)] == ["map/Lighting.sgfl"]
    assert (isolatedCwd / "sglib/Gui/SharedGui.sgfl").exists()
    assert (isolatedCwd / "map/OldName.sgfl").exists()


def test_count_blocks_ignores_sidecar_markers_and_heredoc_bodies():
    text = (
        b"[.] Lighting\n"
        b"Brightness = 2\n"
        b"[Part] Part\n"
        b"Source = <<<\n"
        b"\t[not a block]\n"
        b"\t>>>\n"
        b"[Mesh] MeshPart !blob\n"
        b"!data = AAAA\n"
        b"\n"
        b"[!sidecar .]\n"
        b"Technology = 3\n"
    )
    assert cloud._countBlocks(text) == 3
    assert cloud._countBlocks(b"[.] Lighting\n") == 1
    assert cloud._countBlocks(b"") == 0


def test_emptied_entries_flags_text_entry_that_lost_every_child(isolatedCwd):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n[Fog] Atmosphere\n")

    emptied = cloud._emptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n"})

    assert len(emptied) == 1
    assert "Lighting" in emptied[0] and "3 blocks -> 1" in emptied[0]


def test_emptied_entries_ignores_entry_that_still_has_content(isolatedCwd):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")

    files = {"map/Lighting.sgfl": b"[.] Lighting\n[Sky] Sky\n[Fog] Atmosphere\n"}
    assert cloud._emptiedEntries(entries, files) == []


def test_emptied_entries_ignores_first_save_of_a_new_entry(isolatedCwd):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    assert cloud._emptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n"}) == []


def test_emptied_entries_ignores_entry_that_failed_to_project(isolatedCwd):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")

    # projection warned and produced nothing for this entry — its files stand
    assert cloud._emptiedEntries(entries, {}) == []


def test_emptied_entries_flags_blob_entry_that_lost_its_children_blob(isolatedCwd):
    entries = [{"name": "Workspace", "folder": "map", "format": "blob", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl", "[.] Workspace\n")
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl.rbxm", b"\x00blob\x00")

    emptied = cloud._emptiedEntries(entries, {"map/Workspace.sgfl": b"[.] Workspace\n"})

    assert len(emptied) == 1
    assert "map/Workspace.sgfl.rbxm" in emptied[0]


def test_emptied_entries_ignores_blob_entry_that_still_has_a_blob(isolatedCwd):
    entries = [{"name": "Workspace", "folder": "map", "format": "blob", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl", "[.] Workspace\n")
    _writeAssetFile(isolatedCwd, "map/Workspace.sgfl.rbxm", b"\x00blob\x00")

    files = {"map/Workspace.sgfl": b"[.] Workspace\n", "map/Workspace.sgfl.rbxm": b"\x00new\x00"}
    assert cloud._emptiedEntries(entries, files) == []


def test_emptied_entries_flags_children_entry_that_lost_every_child(isolatedCwd):
    entries = [{"name": "Modules", "folder": "src/Modules", "format": "text", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildB.sgfl.rbxm", b"blob")

    emptied = cloud._emptiedEntries(entries, {"src/Modules/Modules.sgfl": b"[.] Folder\n"})

    assert len(emptied) == 1
    assert "2 child file(s) -> 0" in emptied[0]


def test_emptied_entries_ignores_children_entry_that_still_has_children(isolatedCwd):
    entries = [{"name": "Modules", "folder": "src/Modules", "format": "text", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")

    files = {"src/Modules/Modules.sgfl": b"[.] Folder\n", "src/Modules/ChildA.sgfl": b"[.] ModuleScript\n"}
    assert cloud._emptiedEntries(entries, files) == []


def test_emptied_entries_children_root_block_count_is_not_a_signal(isolatedCwd):
    """A children-mode root file is always a single block (container props
    only) — it must never be read as "emptied"."""
    entries = [{"name": "Modules", "folder": "src/Modules", "format": "text", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")

    files = {"src/Modules/Modules.sgfl": b"[.] Folder\n", "src/Modules/ChildA.sgfl": b"[.] ModuleScript\n"}
    assert cloud._emptiedEntries(entries, files) == []


def test_guard_emptied_entries_aborts_non_interactive_session(isolatedCwd):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")

    with pytest.raises(SGFLError):
        cloud._guardEmptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n"}, 41)


def test_guard_emptied_entries_aborts_when_user_declines(isolatedCwd, monkeypatch):
    import sys

    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())
    monkeypatch.setattr(cloud, "confirmToggle", lambda *args, **kwargs: False)

    with pytest.raises(SGFLError):
        cloud._guardEmptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n"}, 41)


def test_guard_emptied_entries_proceeds_when_user_confirms(isolatedCwd, monkeypatch):
    import sys

    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")
    monkeypatch.setattr(sys, "stdin", _FakeTtyStdin())
    monkeypatch.setattr(cloud, "confirmToggle", lambda *args, **kwargs: True)

    cloud._guardEmptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n"}, 41)


def test_guard_emptied_entries_silent_on_a_normal_save(isolatedCwd, capsys):
    entries = [{"name": "Lighting", "folder": "map", "format": "text", "mode": "file"}]
    _writeAssetFile(isolatedCwd, "map/Lighting.sgfl", "[.] Lighting\n[Sky] Sky\n")

    cloud._guardEmptiedEntries(entries, {"map/Lighting.sgfl": b"[.] Lighting\n[Sky] Sky\n"}, 41)

    assert capsys.readouterr().out == ""


def test_post_save_version_prefers_engine_reported_version(capsys):
    assert cloud._postSaveVersion({"savedVersion": 42}, 41) == 42
    assert capsys.readouterr().out == ""


def test_post_save_version_warns_when_another_version_landed(capsys):
    assert cloud._postSaveVersion({"savedVersion": 44}, 41) == 44
    assert "while the apply task ran" in capsys.readouterr().out


def test_post_save_version_falls_back_when_engine_reports_nothing_useful():
    assert cloud._postSaveVersion({}, 41) == 42
    assert cloud._postSaveVersion({"savedVersion": 41}, 41) == 42  # PlaceVersion not refreshed
    assert cloud._postSaveVersion({"savedVersion": None}, 41) == 42
    assert cloud._postSaveVersion({"savedVersion": "44"}, 41) == 42
    assert cloud._postSaveVersion({"savedVersion": True}, 41) == 42


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


def test_emptied_entries_does_not_count_a_nested_entrys_files_as_children(isolatedCwd):
    """A nested entry's folder can live under a children-mode folder; its files
    must not make the outer entry look populated."""
    entries = [{"name": "Modules", "folder": "src/Modules", "format": "text", "mode": "children"}]
    _writeAssetFile(isolatedCwd, "src/Modules/Modules.sgfl", "[.] Folder\n")
    _writeAssetFile(isolatedCwd, "src/Modules/ChildA.sgfl", "[.] ModuleScript\n")

    files = {
        "src/Modules/Modules.sgfl": b"[.] Folder\n",
        "src/Modules/Nested/Other.sgfl": b"[.] Folder\n",
    }
    emptied = cloud._emptiedEntries(entries, files)

    assert len(emptied) == 1
    assert "1 child file(s) -> 0" in emptied[0]
