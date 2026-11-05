"""An engine with no ML packages, for tests: the same request always gives the same small picture."""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections.abc import Callable
from pathlib import Path

from mia.engine import Cancelled, Progress, Request

SIDE = 8  # small on purpose: nothing about the picture is being tested but its identity


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_bytes(pixels: bytes) -> bytes:
    """An 8-bit RGB PNG, with no chunk beyond the ones a picture needs."""
    rows = b"".join(b"\0" + pixels[row * SIDE * 3 : (row + 1) * SIDE * 3] for row in range(SIDE))
    header = struct.pack(">IIBBBBB", SIDE, SIDE, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


class FakeEngine:
    name = "fake"
    version = "1"

    def generate(self, request: Request, output: Path, progress: Progress, cancelled: Callable[[], bool]) -> str:
        for step in range(1, request.steps + 1):
            progress(step, request.steps)
            if cancelled():
                raise Cancelled(f"stopped at step {step} of {request.steps}")
        identity = repr((request.prompt, request.seed, request.width, request.height, request.steps)).encode()
        pixels = b""
        counter = 0
        while len(pixels) < SIDE * SIDE * 3:
            pixels += hashlib.sha256(identity + bytes([counter])).digest()
            counter += 1
        pixels = pixels[: SIDE * SIDE * 3]
        output.write_bytes(png_bytes(pixels))
        return hashlib.sha256(pixels).hexdigest()
