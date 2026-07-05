"""Synthetic .rbxl byte builders for sidecar.py tests.

Only the pieces sidecar.py actually parses are modeled: a 32-byte header
(contents beyond the magic are never read), then a stream of 16-byte-header
chunks (INST / PROP / END), uncompressed (comp=0) so no lz4/zstd is involved.
"""

import struct

from sgfl import sidecar


def chunk(name: bytes, body: bytes) -> bytes:
    return name + struct.pack("<III", 0, len(body), 0) + body


def instChunk(classId: int, className: str, count: int) -> bytes:
    nameBytes = className.encode("utf-8")
    body = (
        struct.pack("<I", classId)
        + struct.pack("<I", len(nameBytes))
        + nameBytes
        + bytes([0])  # objectFormat, unused by sidecar.py
        + struct.pack("<I", count)
    )
    return chunk(b"INST", body)


def propChunk(classId: int, propName: str, typeId: int, values: list) -> bytes:
    nameBytes = propName.encode("utf-8")
    encoded = sidecar.encodeValues(typeId, values)
    body = (
        struct.pack("<I", classId)
        + struct.pack("<I", len(nameBytes))
        + nameBytes
        + bytes([typeId])
        + encoded
    )
    return chunk(b"PROP", body)


def place(*chunks: bytes) -> bytes:
    header = sidecar.MAGIC + b"\x00" * (32 - len(sidecar.MAGIC))
    return header + b"".join(chunks) + chunk(b"END\x00", b"")
