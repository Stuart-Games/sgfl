"""Sidecar extractor prototype: pull an allowlist of primitive-typed properties
out of a Roblox binary place file (.rbxl) without any reflection database.

Parses only the chunk container (stable since ~2013) and the primitive value
encodings (Bool/Int32/Float32/Float64/Enum — stable since introduction). Every
other chunk and property is skipped as opaque bytes, so unknown new types
elsewhere in the file cannot break this parser.

Usage:
    py sidecar_extract.py <place.rbxl> [--json]
"""

import base64
import json
import struct
import sys

MAGIC = b"<roblox!\x89\xff\x0d\x0a\x1a\x0a"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

# typeId -> name, for diagnostics
TYPE_NAMES = {0x01: "String", 0x02: "Bool", 0x03: "Int32", 0x04: "Float32", 0x05: "Float64", 0x12: "Enum"}

# The allowlist: (className, propertyName) -> expected typeId (None = accept any decodable)
ALLOWLIST = {
    # --- anchors (script-readable, used to validate the decoder) ---
    ("Workspace", "Gravity"): 0x04,
    ("Workspace", "FallenPartsDestroyHeight"): 0x04,
    ("Workspace", "AirDensity"): 0x04,
    ("Workspace", "StreamingEnabled"): 0x02,
    ("Workspace", "ModelStreamingMode"): 0x12,
    ("Workspace", "Retargeting"): 0x12,
    ("Workspace", "ClientAnimatorThrottling"): 0x12,
    # --- targets (NotScriptable — unreachable by any Luau context) ---
    ("Workspace", "StreamingMinRadius"): 0x03,
    ("Workspace", "StreamingTargetRadius"): 0x03,
    ("Workspace", "StreamOutBehavior"): 0x12,
    ("Workspace", "ModelStreamingBehavior"): 0x12,
    ("Workspace", "StreamingIntegrityMode"): 0x12,
    ("Workspace", "PhysicsSteppingMethod"): 0x12,
    ("Workspace", "TouchesUseCollisionGroups"): 0x02,
    ("Workspace", "TouchEventsUseCollisionGroups"): 0x12,  # tri-state enum, not bool
    ("Workspace", "RejectCharacterDeletions"): 0x12,  # tri-state enum, not bool
    # --- opaque blobs (script-unreachable state carried byte-exact) ---
    ("Workspace", "CollisionGroupData"): 0x01,
}

# Allowlist keys that are script-readable: used purely as decoder canaries
# (extracted value must match the in-session value bit-exactly). Everything
# else in ALLOWLIST is sidecar-only (script-unreachable) state.
ANCHORS = {
    ("Workspace", "Gravity"),
    ("Workspace", "FallenPartsDestroyHeight"),
    ("Workspace", "AirDensity"),
    ("Workspace", "StreamingEnabled"),
    ("Workspace", "ModelStreamingMode"),
    ("Workspace", "Retargeting"),
    ("Workspace", "ClientAnimatorThrottling"),
}

# Cosmetic enum value -> name maps (extractor works without them)
ENUM_NAMES = {
    "StreamOutBehavior": {0: "Default", 1: "LowMemory", 2: "Opportunistic"},
    "PhysicsSteppingMethod": {0: "Default", 1: "Fixed", 2: "Adaptive"},
}


def lz4_decompress(data, out_size):
    out = bytearray()
    i = 0
    while i < len(data) and len(out) < out_size:
        token = data[i]
        i += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = data[i]
                i += 1
                lit_len += b
                if b != 255:
                    break
        out += data[i : i + lit_len]
        i += lit_len
        if i >= len(data):
            break
        offset = struct.unpack_from("<H", data, i)[0]
        i += 2
        match_len = token & 0xF
        if match_len == 15:
            while True:
                b = data[i]
                i += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4
        start = len(out) - offset
        for j in range(match_len):
            out.append(out[start + j])
    return bytes(out)


def read_string(buf, pos):
    n = struct.unpack_from("<I", buf, pos)[0]
    return buf[pos + 4 : pos + 4 + n].decode("utf-8", "replace"), pos + 4 + n


def deinterleave(data, count, width):
    """Roblox stores fixed-width value arrays byte-plane transposed."""
    values = []
    for i in range(count):
        values.append(bytes(data[plane * count + i] for plane in range(width)))
    return values


def decode_values(type_id, body, pos, count):
    """Decode `count` primitive values of `type_id` starting at pos. Returns list or None."""
    if type_id == 0x01:  # String: sequential u32-length-prefixed byte blobs
        out = []
        for _ in range(count):
            n = struct.unpack_from("<I", body, pos)[0]
            out.append(body[pos + 4 : pos + 4 + n])
            pos += 4 + n
        return out
    if type_id == 0x02:  # Bool: raw bytes
        return [bool(body[pos + i]) for i in range(count)]
    if type_id == 0x03:  # Int32: interleaved BE, zigzag
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        out = []
        for raw in raws:
            v = struct.unpack(">I", raw)[0]
            out.append((v >> 1) ^ -(v & 1))
        return out
    if type_id == 0x04:  # Float32: interleaved BE, sign bit rotated to LSB
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        out = []
        for raw in raws:
            v = struct.unpack(">I", raw)[0]
            bits = (v >> 1) | ((v & 1) << 31)
            out.append(struct.unpack(">f", struct.pack(">I", bits))[0])
        return out
    if type_id == 0x05:  # Float64: plain little-endian
        return [struct.unpack_from("<d", body, pos + 8 * i)[0] for i in range(count)]
    if type_id == 0x12:  # Enum: interleaved BE u32
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        return [struct.unpack(">I", raw)[0] for raw in raws]
    return None


def extract(path, allowlist=None):
    """Extract allowlisted properties. `allowlist` defaults to the static
    ALLOWLIST; pass a merged dict to add dynamically discovered hidden props
    (expected typeId None = accept any decodable primitive)."""
    if allowlist is None:
        allowlist = ALLOWLIST
    data = open(path, "rb").read()
    if data[: len(MAGIC)] != MAGIC:
        sys.exit(f"ERROR: not a Roblox binary file (magic mismatch): {path}")

    pos = 32
    class_names = {}  # classId -> name
    class_counts = {}  # classId -> instance count
    wanted_classes = {c for c, _ in allowlist}
    results = {}
    problems = []

    while pos + 16 <= len(data):
        chunk_name = data[pos : pos + 4]
        comp, uncomp = struct.unpack_from("<II", data, pos + 4)
        body_start = pos + 16
        if comp == 0:
            body = data[body_start : body_start + uncomp]
            pos = body_start + uncomp
        else:
            raw = data[body_start : body_start + comp]
            pos = body_start + comp
            if raw[:4] == ZSTD_MAGIC:
                try:
                    from compression import zstd  # Python 3.14+

                    body = zstd.decompress(raw)
                except ImportError:
                    problems.append(f"zstd chunk encountered ({chunk_name!r}) and no zstd module available")
                    continue
            else:
                body = lz4_decompress(raw, uncomp)

        if chunk_name == b"INST":
            class_id = struct.unpack_from("<I", body, 0)[0]
            cname, p = read_string(body, 4)
            count = struct.unpack_from("<I", body, p + 1)[0]  # skip u8 objectFormat
            class_names[class_id] = cname
            class_counts[class_id] = count
        elif chunk_name == b"PROP":
            class_id = struct.unpack_from("<I", body, 0)[0]
            cname = class_names.get(class_id)
            if cname not in wanted_classes:
                continue
            pname, p = read_string(body, 4)
            key = (cname, pname)
            if key not in allowlist:
                continue
            type_id = body[p]
            p += 1
            count = class_counts.get(class_id, 0)
            expected = allowlist[key]
            if expected is not None and type_id != expected:
                problems.append(
                    f"{cname}.{pname}: type changed! expected 0x{expected:02x} "
                    f"({TYPE_NAMES.get(expected, '?')}), file has 0x{type_id:02x} "
                    f"({TYPE_NAMES.get(type_id, 'unknown')}) — skipping"
                )
                continue
            values = decode_values(type_id, body, p, count)
            if values is None:
                problems.append(f"{cname}.{pname}: undecodable type 0x{type_id:02x}")
                continue
            results[key] = {"typeId": type_id, "values": values}
        elif chunk_name == b"END\x00":
            break

    # Report allowlist entries that never appeared (property not serialized / renamed)
    for key in allowlist:
        if key not in results and not any(key[1] in prob for prob in problems):
            problems.append(f"{key[0]}.{key[1]}: not found in file (renamed, removed, or default-omitted)")

    return results, problems


def main():
    path = sys.argv[1]
    as_json = "--json" in sys.argv
    results, problems = extract(path)

    if as_json:
        payload = {
            f"{c}.{p}": {
                "type": TYPE_NAMES.get(r["typeId"], hex(r["typeId"])),
                "values": [
                    {"base64": base64.b64encode(v).decode("ascii")} if isinstance(v, bytes) else v
                    for v in r["values"]
                ],
            }
            for (c, p), r in results.items()
        }
        print(json.dumps({"extracted": payload, "problems": problems}, indent=2))
        return

    print(f"=== sidecar extraction: {path}")
    for (cname, pname), r in sorted(results.items()):
        values = [
            f"<{len(v)} bytes: {base64.b64encode(v).decode('ascii')[:48]}...>" if isinstance(v, bytes) else v
            for v in r["values"]
        ]
        rendered = values[0] if len(values) == 1 else values
        if r["typeId"] == 0x12 and len(values) == 1:
            name = ENUM_NAMES.get(pname, {}).get(values[0])
            if name:
                rendered = f"{values[0]} ({name})"
        print(f"  {cname}.{pname} = {rendered}  [{TYPE_NAMES.get(r['typeId'], hex(r['typeId']))}]")
    for prob in problems:
        print(f"  WARN: {prob}")


if __name__ == "__main__":
    main()
