import pytest

from sgfl import sidecar
from tests.helpers import instChunk, propChunk, place


@pytest.mark.parametrize(
    "typeId,values",
    [
        (0x01, [b"hello", b"", b"world with spaces"]),  # String
        (0x02, [True, False, True]),  # Bool
        (0x03, [0, -1, 1, 2147483647, -2147483648]),  # Int32
        (0x04, [0.0, 1.5, -1.5, 3.1415927]),  # Float32
        (0x05, [0.0, 1.5, -1.5, 1e300]),  # Float64
        (0x12, [0, 1, 4294967295]),  # Enum
    ],
)
def test_encode_decode_values_roundtrip(typeId, values):
    encoded = sidecar.encodeValues(typeId, values)
    decoded = sidecar.decodeValues(typeId, encoded, 0, len(values))
    if typeId == 0x04:
        for a, b in zip(values, decoded):
            assert a == pytest.approx(b, rel=1e-6)
    else:
        assert decoded == values


def test_decode_values_unknown_type_returns_none():
    assert sidecar.decodeValues(0xFF, b"", 0, 0) is None


def test_encode_values_unknown_type_returns_none():
    assert sidecar.encodeValues(0xFF, [1, 2]) is None


@pytest.mark.parametrize(
    "typeId,value",
    [
        (0x01, b"binary\x00blob"),
        (0x02, True),
        (0x02, False),
        (0x03, -7),
        (0x12, 3),
        (0x04, 1.5),
        (0x05, 2.25),
    ],
)
def test_render_parse_sidecar_value_roundtrip(typeId, value):
    rendered = sidecar.renderSidecarValue(typeId, value)
    parsed = sidecar.parseSidecarValue(rendered)
    assert parsed == value


def test_extract_reads_allowlisted_prop():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x04, [196.2]),
    )
    allowlist = {("Workspace", "Gravity"): 0x04}
    results, problems = sidecar.extract(data, allowlist)
    assert problems == []
    assert results[("Workspace", "Gravity")]["values"][0] == pytest.approx(196.2, rel=1e-5)


def test_extract_ignores_non_allowlisted_prop():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x04, [196.2]),
        propChunk(0, "Name", 0x01, [b"Workspace"]),
    )
    allowlist = {("Workspace", "Gravity"): 0x04}
    results, problems = sidecar.extract(data, allowlist)
    assert ("Workspace", "Name") not in results
    assert problems == []


def test_extract_flags_missing_allowlisted_prop():
    data = place(instChunk(0, "Workspace", 1))
    allowlist = {("Workspace", "Gravity"): 0x04}
    results, problems = sidecar.extract(data, allowlist)
    assert results == {}
    assert any("not found" in p for p in problems)


def test_extract_flags_type_mismatch():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x02, [True]),  # allowlist expects Float32
    )
    allowlist = {("Workspace", "Gravity"): 0x04}
    results, problems = sidecar.extract(data, allowlist)
    assert ("Workspace", "Gravity") not in results
    assert any("type changed" in p for p in problems)


def test_extract_none_typeid_accepts_any_decodable_type():
    data = place(
        instChunk(0, "Lighting", 1),
        propChunk(0, "Technology", 0x12, [2]),
    )
    allowlist = {("Lighting", "Technology"): None}
    results, problems = sidecar.extract(data, allowlist)
    assert problems == []
    assert results[("Lighting", "Technology")]["values"] == [2]


def test_extract_rejects_bad_magic():
    results, problems = sidecar.extract(b"not a place file at all")
    assert results == {}
    assert "magic mismatch" in problems[0]


def test_patch_file_round_trips_value():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x04, [196.2]),
    )
    patched, applied, problems = sidecar.patchFile(data, {("Workspace", "Gravity"): [-99.0]})
    assert problems == []
    assert applied == {("Workspace", "Gravity")}

    results, _ = sidecar.extract(patched, {("Workspace", "Gravity"): 0x04})
    assert results[("Workspace", "Gravity")]["values"][0] == pytest.approx(-99.0, rel=1e-5)


def test_patch_file_preserves_untouched_chunks():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x04, [196.2]),
        propChunk(0, "Name", 0x01, [b"Workspace"]),
    )
    patched, applied, _ = sidecar.patchFile(data, {("Workspace", "Gravity"): [1.0]})
    results, _ = sidecar.extract(patched, {("Workspace", "Gravity"): 0x04, ("Workspace", "Name"): 0x01})
    assert results[("Workspace", "Name")]["values"][0] == b"Workspace"


def test_patch_file_type_mismatch_not_applied():
    data = place(
        instChunk(0, "Workspace", 1),
        propChunk(0, "Gravity", 0x02, [True]),  # file has Bool, ALLOWLIST says Float32
    )
    patched, applied, problems = sidecar.patchFile(data, {("Workspace", "Gravity"): [1.0]})
    assert applied == set()
    assert any("type changed" in p for p in problems)
    assert patched == data


def test_patch_file_count_mismatch_not_applied():
    data = place(
        instChunk(0, "Workspace", 2),
        propChunk(0, "Gravity", 0x04, [196.2, 196.2]),
    )
    patched, applied, problems = sidecar.patchFile(data, {("Workspace", "Gravity"): [1.0]})
    assert applied == set()
    assert any("value count mismatch" in p for p in problems)


def test_patch_file_missing_prop_reported():
    data = place(instChunk(0, "Workspace", 1))
    _, applied, problems = sidecar.patchFile(data, {("Workspace", "Gravity"): [1.0]})
    assert applied == set()
    assert any("no PROP chunk in file" in p for p in problems)


def test_patch_file_rejects_bad_magic():
    patched, applied, problems = sidecar.patchFile(b"nope", {("Workspace", "Gravity"): [1.0]})
    assert patched == b"nope"
    assert applied == set()
    assert "magic mismatch" in problems[0]


def test_lz4_decompress_literal_only_block():
    # token 0x30 = litLen 3, matchLen 0 (never used since output already at outSize)
    literal = b"abc"
    token = bytes([len(literal) << 4])
    block = token + literal
    assert sidecar.lz4Decompress(block, len(literal)) == literal


def test_zstd_decompress_roundtrip_via_fallback_package():
    zstandard = pytest.importorskip("zstandard")
    payload = b"hello zstd world" * 10
    frame = zstandard.ZstdCompressor().compress(payload)
    assert sidecar.zstdDecompress(frame, len(payload)) == payload


def test_deinterleave_interleave_roundtrip():
    raws = [b"\x01\x02\x03\x04", b"\x05\x06\x07\x08", b"\x09\x0a\x0b\x0c"]
    interleaved = sidecar.interleave(raws, len(raws), 4)
    assert sidecar.deinterleave(interleaved, len(raws), 4) == raws
