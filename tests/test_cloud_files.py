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
