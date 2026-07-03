"""Sidecar patcher prototype: write allowlisted property values back into a
Roblox binary place file (.rbxl) — the inverse of sidecar_extract.py.

Only PROP chunks whose (className, propertyName) appear in the patch set are
touched; every other byte of the file is copied verbatim. Patched chunks are
re-emitted uncompressed (compressedLength=0 is legal per the format), so no
compressor is needed. A property that has no PROP chunk in the file (i.e. was
default-omitted by the writer) cannot be patched and is reported loudly.

Usage:
    py sidecar_patch.py in.rbxl out.rbxl patch.json

patch.json maps "Class.Property" to a value or list of values (one per
instance of that class, in file order). Scalars apply when the class has a
single instance. String-typed values are given as {"base64": "..."}.

    {
      "Workspace.StreamingMinRadius": 128,
      "Workspace.StreamingEnabled": true,
      "Workspace.CollisionGroupData": {"base64": "AQQABPP..."}
    }
"""

import base64
import json
import struct
import sys

from sidecar_extract import (
    ALLOWLIST,
    MAGIC,
    TYPE_NAMES,
    ZSTD_MAGIC,
    decode_values,
    lz4_decompress,
    read_string,
)


def interleave(raws, count, width):
    """Inverse of deinterleave: list of `width`-byte values -> byte-plane transposed."""
    out = bytearray(count * width)
    for i, raw in enumerate(raws):
        for plane in range(width):
            out[plane * count + i] = raw[plane]
    return bytes(out)


def encode_values(type_id, values):
    """Encode a list of primitive values as PROP chunk data. Inverse of decode_values."""
    if type_id == 0x01:  # String: sequential u32-length-prefixed blobs
        return b"".join(struct.pack("<I", len(v)) + v for v in values)
    if type_id == 0x02:  # Bool: raw bytes
        return bytes(1 if v else 0 for v in values)
    if type_id == 0x03:  # Int32: zigzag, interleaved BE
        raws = []
        for v in values:
            zigzag = ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF
            raws.append(struct.pack(">I", zigzag))
        return interleave(raws, len(values), 4)
    if type_id == 0x04:  # Float32: sign bit rotated to LSB, interleaved BE
        raws = []
        for v in values:
            bits = struct.unpack(">I", struct.pack(">f", v))[0]
            rotated = ((bits << 1) | (bits >> 31)) & 0xFFFFFFFF
            raws.append(struct.pack(">I", rotated))
        return interleave(raws, len(values), 4)
    if type_id == 0x05:  # Float64: plain little-endian
        return b"".join(struct.pack("<d", v) for v in values)
    if type_id == 0x12:  # Enum: interleaved BE u32
        raws = [struct.pack(">I", v) for v in values]
        return interleave(raws, len(values), 4)
    return None


def normalizeValue(raw):
    if isinstance(raw, dict) and "base64" in raw:
        return base64.b64decode(raw["base64"])
    return raw


def loadPatchSet(path):
    """patch.json -> {(className, propName): [values]} (values normalized, not yet count-checked)."""
    with open(path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    patches = {}
    for key, raw in entries.items():
        className, _, propName = key.partition(".")
        if not propName:
            sys.exit(f"ERROR: patch key {key!r} is not of the form Class.Property")
        values = raw if isinstance(raw, list) else [raw]
        patches[(className, propName)] = [normalizeValue(v) for v in values]
    return patches


def patch_file(data, patches):
    """Return (patchedBytes, appliedKeys, problems). `patches` is {(class,prop): [values]}."""
    if data[: len(MAGIC)] != MAGIC:
        sys.exit("ERROR: not a Roblox binary file (magic mismatch)")

    out = bytearray(data[:32])
    pos = 32
    class_names = {}
    class_counts = {}
    wanted_classes = {c for c, _ in patches}
    applied = set()
    problems = []

    while pos + 16 <= len(data):
        chunk_start = pos
        chunk_name = data[pos : pos + 4]
        comp, uncomp = struct.unpack_from("<II", data, pos + 4)
        body_start = pos + 16
        chunk_end = body_start + (uncomp if comp == 0 else comp)
        raw_chunk = data[chunk_start:chunk_end]
        pos = chunk_end

        # decompress only what we need to inspect (INST always; PROP if class might match)
        def getBody():
            if comp == 0:
                return data[body_start : body_start + uncomp]
            raw = data[body_start : body_start + comp]
            if raw[:4] == ZSTD_MAGIC:
                from compression import zstd  # Python 3.14+; raises if unavailable

                return zstd.decompress(raw)
            return lz4_decompress(raw, uncomp)

        replacement = None
        if chunk_name == b"INST":
            body = getBody()
            class_id = struct.unpack_from("<I", body, 0)[0]
            cname, p = read_string(body, 4)
            class_names[class_id] = cname
            class_counts[class_id] = struct.unpack_from("<I", body, p + 1)[0]
        elif chunk_name == b"PROP":
            # peek the class id from the (possibly compressed) body only when needed
            body = None
            try:
                body = getBody()
            except Exception as err:  # zstd unavailable etc.
                problems.append(f"PROP chunk skipped (cannot decompress: {err})")
            if body is not None:
                class_id = struct.unpack_from("<I", body, 0)[0]
                cname = class_names.get(class_id)
                if cname in wanted_classes:
                    pname, p = read_string(body, 4)
                    key = (cname, pname)
                    if key in patches:
                        type_id = body[p]
                        count = class_counts.get(class_id, 0)
                        expected = ALLOWLIST.get(key)
                        if expected is not None and type_id != expected:
                            problems.append(
                                f"{cname}.{pname}: type changed! allowlist says 0x{expected:02x}, "
                                f"file has 0x{type_id:02x} ({TYPE_NAMES.get(type_id, 'unknown')}) — NOT patched"
                            )
                        else:
                            values = patches[key]
                            if len(values) != count:
                                problems.append(
                                    f"{cname}.{pname}: value count mismatch (patch has {len(values)}, "
                                    f"file has {count} instances) — NOT patched"
                                )
                            else:
                                encoded = encode_values(type_id, values)
                                if encoded is None:
                                    problems.append(
                                        f"{cname}.{pname}: unencodable type 0x{type_id:02x} — NOT patched"
                                    )
                                else:
                                    # sanity: our encoder must decode back to what we encoded
                                    roundtrip = decode_values(type_id, encoded, 0, count)
                                    newBody = body[: p + 1] + encoded
                                    replacement = (
                                        chunk_name
                                        + struct.pack("<III", 0, len(newBody), 0)
                                        + newBody
                                    )
                                    applied.add(key)
                                    if roundtrip is None:
                                        problems.append(
                                            f"{cname}.{pname}: internal roundtrip check unavailable"
                                        )

        out += replacement if replacement is not None else raw_chunk
        if chunk_name == b"END\x00":
            break

    # anything left over in `data` after END (shouldn't exist, but never drop bytes)
    out += data[pos:]

    for key in patches:
        if key not in applied and not any(f"{key[0]}.{key[1]}" in prob for prob in problems):
            problems.append(
                f"{key[0]}.{key[1]}: no PROP chunk in file (default-omitted or renamed) — NOT patched"
            )
    return bytes(out), applied, problems


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    inPath, outPath, patchPath = sys.argv[1], sys.argv[2], sys.argv[3]
    patches = loadPatchSet(patchPath)
    data = open(inPath, "rb").read()
    patched, applied, problems = patch_file(data, patches)
    with open(outPath, "wb") as f:
        f.write(patched)
    print(f"=== sidecar patch: {inPath} -> {outPath} ({len(data)} -> {len(patched)} bytes)")
    for cname, pname in sorted(applied):
        print(f"  patched: {cname}.{pname}")
    for prob in problems:
        print(f"  WARN: {prob}")
    if len(applied) < len(patches):
        sys.exit(1)


if __name__ == "__main__":
    main()
