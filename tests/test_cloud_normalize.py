import pytest

from sgfl import cloud
from sgfl.util import SGFLError


def test_defaults_applied():
    table = cloud.normalizeAssetConfig(
        {"Workspace": {"folder": "src/Workspace", "robloxPath": "Workspace"}}
    )
    entry = table["Workspace"]
    assert entry["format"] == "blob"  # Workspace is a BLOB_DEFAULT_NAME
    assert entry["mode"] == "file"
    assert entry["include"] == []
    assert entry["exclude"] == []
    assert entry["pathSegments"] == ["Workspace"]


def test_non_blob_default_name_defaults_to_text():
    table = cloud.normalizeAssetConfig(
        {"Lighting": {"folder": "src/Lighting", "robloxPath": "Lighting"}}
    )
    assert table["Lighting"]["format"] == "text"


def test_missing_folder_rejected():
    with pytest.raises(SGFLError):
        cloud.normalizeAssetConfig({"X": {"robloxPath": "Workspace"}})


def test_missing_roblox_path_rejected():
    with pytest.raises(SGFLError):
        cloud.normalizeAssetConfig({"X": {"folder": "src/X"}})


def test_unknown_key_rejected():
    with pytest.raises(SGFLError, match="unknown key"):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "Workspace", "bogus": True}}
        )


def test_bad_format_value_rejected():
    with pytest.raises(SGFLError):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "Workspace", "format": "xml"}}
        )


def test_bad_mode_value_rejected():
    with pytest.raises(SGFLError):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "Workspace", "mode": "bogus"}}
        )


def test_v2_feature_without_version_rejected():
    with pytest.raises(SGFLError, match="\\$version"):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "Workspace", "mode": "children"}}
        )


def test_v2_feature_with_version_2_accepted():
    table = cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "X": {"folder": "src/X", "robloxPath": "Workspace", "mode": "children"},
        }
    )
    assert table["X"]["mode"] == "children"


def test_deep_roblox_path_requires_version_2():
    with pytest.raises(SGFLError, match="\\$version"):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "ServerScriptService.Foo.Bar"}}
        )


def test_version_newer_than_supported_rejected():
    with pytest.raises(SGFLError, match="does not understand"):
        cloud.normalizeAssetConfig(
            {"$version": cloud.SUPPORTED_ASSET_VERSION + 1, "X": {"folder": "src/X", "robloxPath": "Workspace"}}
        )


def test_version_zero_rejected():
    with pytest.raises(SGFLError, match=">= 1"):
        cloud.normalizeAssetConfig({"$version": 0})


def test_version_non_int_rejected():
    with pytest.raises(SGFLError, match="integer"):
        cloud.normalizeAssetConfig({"$version": "2"})


def test_children_mode_folder_must_be_exclusive():
    with pytest.raises(SGFLError, match="must be dedicated"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/Shared", "robloxPath": "Workspace", "mode": "children"},
                "B": {"folder": "src/Shared", "robloxPath": "Lighting"},
            }
        )


def test_overlapping_roblox_paths_rejected_equal():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "A": {"folder": "src/A", "robloxPath": "Workspace"},
                "B": {"folder": "src/B", "robloxPath": "Workspace"},
            }
        )


def test_overlapping_roblox_paths_rejected_prefix():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/A", "robloxPath": "ServerScriptService"},
                "B": {"folder": "src/B", "robloxPath": "ServerScriptService.Sub"},
            }
        )


def test_overlap_allowed_when_ancestor_excludes_delegated_child():
    table = cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "A": {
                "folder": "src/A",
                "robloxPath": "ServerScriptService",
                "mode": "children",
                "exclude": ["Sub"],
            },
            "B": {"folder": "src/B", "robloxPath": "ServerScriptService.Sub", "mode": "children"},
        }
    )
    assert set(table.keys()) == {"A", "B"}


def test_overlap_allowed_via_glob_star_exclude():
    table = cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "A": {"folder": "src/A", "robloxPath": "StarterGui", "exclude": ["Sh*"]},
            "B": {"folder": "src/B", "robloxPath": "StarterGui.Shared", "mode": "children"},
        }
    )
    assert set(table.keys()) == {"A", "B"}


def test_overlap_still_rejected_when_exclude_does_not_match():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/A", "robloxPath": "ServerScriptService", "exclude": ["Other"]},
                "B": {"folder": "src/B", "robloxPath": "ServerScriptService.Sub"},
            }
        )


def test_overlap_still_rejected_with_classname_only_exclude():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/A", "robloxPath": "ServerScriptService", "exclude": ["$Folder"]},
                "B": {"folder": "src/B", "robloxPath": "ServerScriptService.Sub"},
            }
        )


def test_overlap_rejected_equal_paths_even_with_exclude():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/A", "robloxPath": "Workspace", "exclude": ["Anything"]},
                "B": {"folder": "src/B", "robloxPath": "Workspace"},
            }
        )


def test_overlap_delegation_three_levels_needs_exclude_at_every_direct_link():
    # each entry that would otherwise directly own the delegated child must
    # exclude it — A excludes "Shared" (its own direct child), and B (which
    # owns "Shared" itself) must separately exclude "Toolbar" for C to be legal
    table = cloud.normalizeAssetConfig(
        {
            "$version": 2,
            "A": {"folder": "src/A", "robloxPath": "StarterGui", "exclude": ["Shared"]},
            "B": {
                "folder": "src/B",
                "robloxPath": "StarterGui.Shared",
                "mode": "children",
                "exclude": ["Toolbar"],
            },
            "C": {"folder": "src/C", "robloxPath": "StarterGui.Shared.Toolbar"},
        }
    )
    assert set(table.keys()) == {"A", "B", "C"}


def test_overlap_delegation_three_levels_rejected_if_middle_link_missing_exclude():
    with pytest.raises(SGFLError, match="overlapping robloxPath"):
        cloud.normalizeAssetConfig(
            {
                "$version": 2,
                "A": {"folder": "src/A", "robloxPath": "StarterGui", "exclude": ["Shared"]},
                "B": {"folder": "src/B", "robloxPath": "StarterGui.Shared", "mode": "children"},
                "C": {"folder": "src/C", "robloxPath": "StarterGui.Shared.Toolbar"},
            }
        )


def test_disjoint_roblox_paths_allowed():
    table = cloud.normalizeAssetConfig(
        {
            "A": {"folder": "src/A", "robloxPath": "Workspace"},
            "B": {"folder": "src/B", "robloxPath": "Lighting"},
        }
    )
    assert set(table.keys()) == {"A", "B"}


def test_shared_folder_allowed_for_two_file_mode_entries():
    table = cloud.normalizeAssetConfig(
        {
            "A": {"folder": "src/Shared", "robloxPath": "Workspace"},
            "B": {"folder": "src/Shared", "robloxPath": "Lighting"},
        }
    )
    assert table["A"]["folder"] == table["B"]["folder"] == "src/Shared"


def test_include_exclude_must_be_string_lists():
    with pytest.raises(SGFLError):
        cloud.normalizeAssetConfig(
            {"X": {"folder": "src/X", "robloxPath": "Workspace", "include": "Temp*"}}
        )


def test_rojo_mounted_paths_finds_dollar_path_mounts():
    projectJson = {
        "tree": {
            "$className": "DataModel",
            "ReplicatedFirst": {"$path": "src/ReplicatedFirst"},
            "StarterPlayer": {
                "StarterPlayerScripts": {"$path": "src/StarterPlayerScripts"},
            },
            "Workspace": {"NotMounted": {}},
        }
    }
    paths = cloud.rojoMountedPaths(projectJson)
    assert "ReplicatedFirst" in paths
    assert "StarterPlayer/StarterPlayerScripts" in paths
    assert not any(p.startswith("Workspace") for p in paths)


def test_rojo_mounted_paths_empty_tree():
    assert cloud.rojoMountedPaths({}) == []


def test_pack_unpack_container_roundtrip():
    files = [("a.sgfl", b"hello world"), ("b/c.sgfl.rbxm", b"\x00\x01\x02binary"), ("empty.sgfl", b"")]
    container = cloud.packContainer(files)
    manifest, unpacked = cloud.unpackContainer(container)
    assert unpacked["a.sgfl"] == b"hello world"
    assert unpacked["b/c.sgfl.rbxm"] == b"\x00\x01\x02binary"
    assert unpacked["empty.sgfl"] == b""
    assert len(manifest["files"]) == 3


def test_crlf_structural_line_detected():
    data = b"[path] ClassName\r\nProp = 1\n"
    assert cloud._hasCrlfStructuralLines(data)


def test_crlf_only_in_heredoc_body_allowed():
    # heredoc body lines are tab-prefixed; a raw \r there is legitimate string content
    data = b"[path] ClassName\nSource = <<END\n\tsome text with cr\r\nEND\n"
    assert not cloud._hasCrlfStructuralLines(data)


def test_no_crlf_is_fine():
    data = b"[path] ClassName\nProp = 1\n"
    assert not cloud._hasCrlfStructuralLines(data)
