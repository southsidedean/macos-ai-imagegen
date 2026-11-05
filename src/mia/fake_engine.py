"""An engine with no ML packages, for tests: the same request always gives the same small picture."""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import time
import zlib
from collections.abc import Callable
from pathlib import Path

from mia.engine import Cancelled, Progress, Request

SIDE = 8  # small on purpose: nothing about the picture is being tested but its identity
# One grey pixel, as a real JPEG, because making one would take a package this engine exists to avoid.
PREVIEW = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxl"
    "Z2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAABAAEDASIAAhEB"
    "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS"
    "0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKz"
    "tLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgEC"
    "BAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpj"
    "ZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6"
    "/9oADAMBAAIRAxEAPwAooooA/9k="
)


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
            try:
                time.sleep(float(os.environ.get("MIA_FAKE_STEP_SECONDS", "0")))  # for tests that act mid-job
            except KeyboardInterrupt:
                # What mflux's step loop does, which mia's engine turns into this. The fake has to match, or
                # the tests pass on a way of stopping that the real engine doesn't have.
                raise Cancelled(f"interrupted at step {step} of {request.steps}") from None
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

    def preview(self, picture: Path, output: Path) -> None:
        output.write_bytes(PREVIEW)
