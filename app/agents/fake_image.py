"""Fake (demo) image generator – creates a placeholder PNG."""

import struct
import zlib

import structlog

from app.agents.base import ImageGenerator, ImageResult

logger = structlog.get_logger()


def _create_placeholder_png() -> bytes:
    """Create a minimal 4x4 green placeholder PNG in pure Python."""
    width, height = 4, 4
    # RGBA: soft green color
    pixel = b"\x90\xc8\x90\xff"
    raw_data = b""
    for _ in range(height):
        raw_data += b"\x00" + pixel * width  # filter byte + row pixels

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk = chunk_type + data
        return (
            struct.pack(">I", len(data)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
        )

    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr_data)
    png += _chunk(b"IDAT", zlib.compress(raw_data))
    png += _chunk(b"IEND", b"")
    return png


class FakeImageGenerator(ImageGenerator):
    """Demo image generator – writes a small placeholder PNG to disk."""

    async def generate(self, prompt: str) -> ImageResult:
        logger.info("fake_image.generate", mode="demo", prompt_preview=prompt[:50])
        png_data = _create_placeholder_png()
        return ImageResult(data=png_data, status="completed")
