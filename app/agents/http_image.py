"""HTTP-based image generator using OpenAI DALL-E compatible API."""

import uuid

import httpx
import structlog

from app.agents.base import GenerationMeta, ImageGenerator, ImageResult
from app.config import get_settings

logger = structlog.get_logger()


class HttpImageGenerator(ImageGenerator):
    """Production image generator calling OpenAI DALL-E compatible API."""

    def __init__(
        self, client: httpx.AsyncClient | None = None, retry_delay_seconds: float = 2.0
    ) -> None:
        settings = get_settings()
        if not settings.IMAGE_API_KEY or not settings.IMAGE_MODEL or not settings.IMAGE_BASE_URL:
            raise RuntimeError("真实图片模式需要 IMAGE_API_KEY、IMAGE_MODEL 和 IMAGE_BASE_URL")
        self._api_key = settings.IMAGE_API_KEY
        self._model = settings.IMAGE_MODEL
        self._base_url = settings.IMAGE_BASE_URL.rstrip("/")
        self._timeout = settings.IMAGE_TIMEOUT_SECONDS
        self._client = client
        self._retry_delay_seconds = retry_delay_seconds

    async def generate(self, prompt: str) -> ImageResult:
        """Generate an image via API with 1 retry. Returns failed on final error."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "prompt": prompt,
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
        }
        url = f"{self._base_url}/images/generations"

        for attempt in range(2):
            try:
                if self._client:
                    response = await self._client.post(url, json=payload, headers=headers)
                else:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()

                # Decode base64 image
                import base64

                b64_data = data["data"][0]["b64_json"]
                image_bytes = base64.b64decode(b64_data)
                if not self._is_valid_image(image_bytes):
                    raise ValueError("图片模型未返回有效的 PNG/JPEG/WebP 图片")

                generation = GenerationMeta(
                    source="remote",
                    model=self._model,
                    request_id=str(data.get("id") or uuid.uuid4()),
                )
                logger.info(
                    "http_image.generation_completed",
                    source=generation.source,
                    model=generation.model,
                    request_id=generation.request_id,
                    byte_count=len(image_bytes),
                )
                return ImageResult(
                    data=image_bytes,
                    status="completed",
                    generation=generation,
                )

            except Exception as e:
                logger.warning(
                    "http_image.generate.failed",
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt == 0:
                    import asyncio

                    await asyncio.sleep(self._retry_delay_seconds)
                else:
                    return ImageResult(
                        data=None,
                        status="failed",
                        error=str(e),
                    )

        # Unreachable, but satisfy type checker
        return ImageResult(data=None, status="failed", error="unexpected")

    @staticmethod
    def _is_valid_image(data: bytes) -> bool:
        dimensions = HttpImageGenerator._image_dimensions(data)
        return dimensions is not None and dimensions[0] >= 64 and dimensions[1] >= 64

    @staticmethod
    def _image_dimensions(data: bytes) -> tuple[int, int] | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            if len(data) < 24:
                return None
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data.startswith(b"\xff\xd8\xff"):
            offset = 2
            start_of_frame = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB}
            while offset + 8 < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                if marker in start_of_frame:
                    height = int.from_bytes(data[offset + 5 : offset + 7], "big")
                    width = int.from_bytes(data[offset + 7 : offset + 9], "big")
                    return width, height
                if marker in {0xD8, 0xD9}:
                    offset += 2
                    continue
                segment_length = int.from_bytes(data[offset + 2 : offset + 4], "big")
                if segment_length < 2:
                    return None
                offset += 2 + segment_length
            return None
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X" and len(data) >= 30:
                width = 1 + int.from_bytes(data[24:27], "little")
                height = 1 + int.from_bytes(data[27:30], "little")
                return width, height
            if data[12:16] == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
                bits = int.from_bytes(data[21:25], "little")
                return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
            if data[12:16] == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
                width = int.from_bytes(data[26:28], "little") & 0x3FFF
                height = int.from_bytes(data[28:30], "little") & 0x3FFF
                return width, height
        return None
