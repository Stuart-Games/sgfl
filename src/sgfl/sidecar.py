"""Binary place-file sidecar: reads and writes script-unreachable properties
directly in the .rbxl chunk container.

Some serialized state cannot cross the Luau boundary in either direction
(NotScriptable streaming settings, CoreScript-gated RBX_* attributes,
Lighting.Technology, Workspace.CollisionGroupData, ...). The sidecar carries
that state byte-exact: `extract` pulls allowlisted values out of a downloaded
place file, and `patchFile` writes values back by re-emitting the affected
PROP chunks uncompressed (legal per the format, so no compressor is needed).

Only the chunk container (stable since ~2013) and the primitive value
encodings (String/Bool/Int32/Float32/Float64/Enum) are parsed; every other
byte is treated as opaque, so format changes elsewhere cannot break this.
All failures are loud (returned in a problems list), never silent.
"""

import base64
import struct

MAGIC = b"<roblox!\x89\xff\x0d\x0a\x1a\x0a"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

TYPE_NAMES = {0x01: "String", 0x02: "Bool", 0x03: "Int32", 0x04: "Float32", 0x05: "Float64", 0x12: "Enum"}

# Static allowlist: (className, propertyName) -> expected typeId. The dynamic
# part of the sidecar (hidden props reported by the projection task) is merged
# on top at call time with expected typeId None (= accept any decodable).
ALLOWLIST = {
    # --- anchors (script-readable; used to validate the decoder every save) ---
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
    # --- engine derives visible state from this at load. Since v2.1.0 the
    # full-capability sweep also reports it dynamically; the static entry
    # remains to pin the expected typeId ---
    ("Lighting", "Technology"): 0x12,  # drives LightingStyle/PrioritizeLightingQuality
}

# Allowlist keys that are script-readable: used purely as decoder canaries
# (extracted value must match the in-session value bit-exactly). Everything
# else is sidecar-only (script-unreachable) state.
ANCHORS = {
    ("Workspace", "Gravity"),
    ("Workspace", "FallenPartsDestroyHeight"),
    ("Workspace", "AirDensity"),
    ("Workspace", "StreamingEnabled"),
    ("Workspace", "ModelStreamingMode"),
    ("Workspace", "Retargeting"),
    ("Workspace", "ClientAnimatorThrottling"),
}


def lz4Decompress(data, outSize):
    out = bytearray()
    i = 0
    while i < len(data) and len(out) < outSize:
        token = data[i]
        i += 1
        litLen = token >> 4
        if litLen == 15:
            while True:
                b = data[i]
                i += 1
                litLen += b
                if b != 255:
                    break
        out += data[i : i + litLen]
        i += litLen
        if i >= len(data):
            break
        offset = struct.unpack_from("<H", data, i)[0]
        i += 2
        matchLen = token & 0xF
        if matchLen == 15:
            while True:
                b = data[i]
                i += 1
                matchLen += b
                if b != 255:
                    break
        matchLen += 4
        start = len(out) - offset
        for j in range(matchLen):
            out.append(out[start + j])
    return bytes(out)


def zstdDecompress(data, outSize):
    """Decompress a zstd-framed chunk body. Prefers the stdlib `compression.zstd`
    (Python 3.14+, no extra dependency); falls back to the `zstandard` package
    on older interpreters. Raises ImportError if neither is available — callers
    treat that as a loud, reported problem rather than silently losing data."""
    try:
        from compression import zstd  # Python 3.14+

        return zstd.decompress(data)
    except ImportError:
        pass
    import zstandard

    return zstandard.ZstdDecompressor().decompress(data, max_output_size=outSize)


def readString(buf, pos):
    n = struct.unpack_from("<I", buf, pos)[0]
    return buf[pos + 4 : pos + 4 + n].decode("utf-8", "replace"), pos + 4 + n


def deinterleave(data, count, width):
    """Roblox stores fixed-width value arrays byte-plane transposed."""
    return [bytes(data[plane * count + i] for plane in range(width)) for i in range(count)]


def interleave(raws, count, width):
    out = bytearray(count * width)
    for i, raw in enumerate(raws):
        for plane in range(width):
            out[plane * count + i] = raw[plane]
    return bytes(out)


def decodeValues(typeId, body, pos, count):
    """Decode `count` primitive values of `typeId` starting at pos. Returns list or None."""
    if typeId == 0x01:  # String: sequential u32-length-prefixed byte blobs
        out = []
        for _ in range(count):
            n = struct.unpack_from("<I", body, pos)[0]
            out.append(body[pos + 4 : pos + 4 + n])
            pos += 4 + n
        return out
    if typeId == 0x02:  # Bool: raw bytes
        return [bool(body[pos + i]) for i in range(count)]
    if typeId == 0x03:  # Int32: interleaved BE, zigzag
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        out = []
        for raw in raws:
            v = struct.unpack(">I", raw)[0]
            out.append((v >> 1) ^ -(v & 1))
        return out
    if typeId == 0x04:  # Float32: interleaved BE, sign bit rotated to LSB
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        out = []
        for raw in raws:
            v = struct.unpack(">I", raw)[0]
            bits = (v >> 1) | ((v & 1) << 31)
            out.append(struct.unpack(">f", struct.pack(">I", bits))[0])
        return out
    if typeId == 0x05:  # Float64: plain little-endian
        return [struct.unpack_from("<d", body, pos + 8 * i)[0] for i in range(count)]
    if typeId == 0x12:  # Enum: interleaved BE u32
        raws = deinterleave(body[pos : pos + 4 * count], count, 4)
        return [struct.unpack(">I", raw)[0] for raw in raws]
    return None


def encodeValues(typeId, values):
    """Encode a list of primitive values as PROP chunk data. Inverse of decodeValues."""
    if typeId == 0x01:
        return b"".join(struct.pack("<I", len(v)) + v for v in values)
    if typeId == 0x02:
        return bytes(1 if v else 0 for v in values)
    if typeId == 0x03:
        raws = [struct.pack(">I", ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF) for v in values]
        return interleave(raws, len(values), 4)
    if typeId == 0x04:
        raws = []
        for v in values:
            bits = struct.unpack(">I", struct.pack(">f", v))[0]
            raws.append(struct.pack(">I", ((bits << 1) | (bits >> 31)) & 0xFFFFFFFF))
        return interleave(raws, len(values), 4)
    if typeId == 0x05:
        return b"".join(struct.pack("<d", v) for v in values)
    if typeId == 0x12:
        raws = [struct.pack(">I", int(v)) for v in values]
        return interleave(raws, len(values), 4)
    return None


def extract(data, allowlist=None):
    """Extract allowlisted properties from place-file bytes.

    Returns ({(className, propName): {"typeId", "values"}}, [problem strings]).
    """
    if allowlist is None:
        allowlist = ALLOWLIST
    if data[: len(MAGIC)] != MAGIC:
        return {}, ["not a Roblox binary file (magic mismatch)"]

    pos = 32
    classNames = {}
    classCounts = {}
    wantedClasses = {c for c, _ in allowlist}
    results = {}
    problems = []

    while pos + 16 <= len(data):
        chunkName = data[pos : pos + 4]
        comp, uncomp = struct.unpack_from("<II", data, pos + 4)
        bodyStart = pos + 16
        if comp == 0:
            body = data[bodyStart : bodyStart + uncomp]
            pos = bodyStart + uncomp
        else:
            raw = data[bodyStart : bodyStart + comp]
            pos = bodyStart + comp
            if raw[:4] == ZSTD_MAGIC:
                try:
                    body = zstdDecompress(raw, uncomp)
                except ImportError:
                    problems.append(
                        f"zstd chunk encountered ({chunkName!r}) and no zstd decoder available "
                        '(pip install "zstandard", or use Python 3.14+)'
                    )
                    continue
            else:
                body = lz4Decompress(raw, uncomp)

        if chunkName == b"INST":
            classId = struct.unpack_from("<I", body, 0)[0]
            cname, p = readString(body, 4)
            classNames[classId] = cname
            classCounts[classId] = struct.unpack_from("<I", body, p + 1)[0]  # skip u8 objectFormat
        elif chunkName == b"PROP":
            classId = struct.unpack_from("<I", body, 0)[0]
            cname = classNames.get(classId)
            if cname not in wantedClasses:
                continue
            pname, p = readString(body, 4)
            key = (cname, pname)
            if key not in allowlist:
                continue
            typeId = body[p]
            p += 1
            count = classCounts.get(classId, 0)
            expected = allowlist[key]
            if expected is not None and typeId != expected:
                problems.append(
                    f"{cname}.{pname}: type changed! expected 0x{expected:02x} "
                    f"({TYPE_NAMES.get(expected, '?')}), file has 0x{typeId:02x} "
                    f"({TYPE_NAMES.get(typeId, 'unknown')}) — skipping"
                )
                continue
            values = decodeValues(typeId, body, p, count)
            if values is None:
                problems.append(f"{cname}.{pname}: undecodable type 0x{typeId:02x}")
                continue
            results[key] = {"typeId": typeId, "values": values}
        elif chunkName == b"END\x00":
            break

    for key in allowlist:
        if key not in results and not any(key[1] in prob for prob in problems):
            problems.append(f"{key[0]}.{key[1]}: not found in file (renamed, removed, or default-omitted)")

    return results, problems


def patchFile(data, patches):
    """Write values back into place-file bytes.

    `patches` is {(className, propName): [values]} (one value per instance of
    the class, in file order). Returns
    (patchedBytes, appliedKeys, problems, omitted). `problems` are fatal —
    something was wrong with a value we could have written. `omitted` are
    properties the engine did not serialize at all, which cannot be written
    and are reported rather than fatal; see the tail of this function.
    Only matching PROP chunks are touched; everything else is copied verbatim.
    """
    if data[: len(MAGIC)] != MAGIC:
        return data, set(), ["not a Roblox binary file (magic mismatch)"], []

    out = bytearray(data[:32])
    pos = 32
    classNames = {}
    classCounts = {}
    wantedClasses = {c for c, _ in patches}
    applied = set()
    problems = []
    omitted = []

    while pos + 16 <= len(data):
        chunkStart = pos
        chunkName = data[pos : pos + 4]
        comp, uncomp = struct.unpack_from("<II", data, pos + 4)
        bodyStart = pos + 16
        chunkEnd = bodyStart + (uncomp if comp == 0 else comp)
        rawChunk = data[chunkStart:chunkEnd]
        pos = chunkEnd

        def getBody():
            if comp == 0:
                return data[bodyStart : bodyStart + uncomp]
            raw = data[bodyStart : bodyStart + comp]
            if raw[:4] == ZSTD_MAGIC:
                return zstdDecompress(raw, uncomp)  # raises ImportError if no zstd decoder is available
            return lz4Decompress(raw, uncomp)

        replacement = None
        if chunkName == b"INST":
            body = getBody()
            classId = struct.unpack_from("<I", body, 0)[0]
            cname, p = readString(body, 4)
            classNames[classId] = cname
            classCounts[classId] = struct.unpack_from("<I", body, p + 1)[0]
        elif chunkName == b"PROP":
            body = None
            try:
                body = getBody()
            except Exception as err:
                problems.append(f"PROP chunk skipped (cannot decompress: {err})")
            if body is not None:
                classId = struct.unpack_from("<I", body, 0)[0]
                cname = classNames.get(classId)
                if cname in wantedClasses:
                    pname, p = readString(body, 4)
                    key = (cname, pname)
                    if key in patches:
                        typeId = body[p]
                        count = classCounts.get(classId, 0)
                        expected = ALLOWLIST.get(key)
                        values = patches[key]
                        if expected is not None and typeId != expected:
                            problems.append(
                                f"{cname}.{pname}: type changed! allowlist says 0x{expected:02x}, "
                                f"file has 0x{typeId:02x} ({TYPE_NAMES.get(typeId, 'unknown')}) — NOT patched"
                            )
                        elif len(values) != count:
                            problems.append(
                                f"{cname}.{pname}: value count mismatch (patch has {len(values)}, "
                                f"file has {count} instances) — NOT patched"
                            )
                        else:
                            encoded = encodeValues(typeId, values)
                            if encoded is None:
                                problems.append(
                                    f"{cname}.{pname}: unencodable type 0x{typeId:02x} — NOT patched"
                                )
                            else:
                                newBody = body[: p + 1] + encoded
                                replacement = chunkName + struct.pack("<III", 0, len(newBody), 0) + newBody
                                applied.add(key)

        out += replacement if replacement is not None else rawChunk
        if chunkName == b"END\x00":
            break

    out += data[pos:]  # never drop trailing bytes

    presentClasses = set(classNames.values())
    for key in patches:
        if key in applied or any(f"{key[0]}.{key[1]}" in prob for prob in problems):
            continue
        if key[0] in presentClasses:
            # The class is in the file, so the subtree built fine — the engine
            # simply serialized this property at its default and omitted it.
            # There is no chunk to rewrite and no way to insert one, so failing
            # here does not protect anything: it just makes the project
            # permanently unbuildable. Report it and keep the engine's value.
            omitted.append(
                f"{key[0]}.{key[1]}: not serialized in the applied place "
                f"(engine default) — keeping the engine's value"
            )
        else:
            # The whole class is missing. That is structural, not a default:
            # something the entry files describe did not survive the apply.
            problems.append(
                f"{key[0]}.{key[1]}: class {key[0]} is absent from the file entirely — NOT patched"
            )
    return bytes(out), applied, problems, omitted


def renderSidecarValue(typeId, value):
    """Render an extracted value as a [!sidecar] block line value."""
    if typeId == 0x01:
        return 'Base64("' + base64.b64encode(value).decode("ascii") + '")'
    if typeId == 0x02:
        return "true" if value else "false"
    if typeId in (0x03, 0x12):
        return str(int(value))
    return repr(float(value))


def parseSidecarValue(text):
    """Inverse of renderSidecarValue."""
    if text == "true":
        return True
    if text == "false":
        return False
    if text.startswith('Base64("') and text.endswith('")'):
        return base64.b64decode(text[8:-2])
    try:
        return int(text)
    except ValueError:
        return float(text)
