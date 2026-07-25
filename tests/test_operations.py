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
