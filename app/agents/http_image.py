"""HTTP-based image generator using OpenAI DALL-E compatible API."""

import httpx
import structlog

from app.agents.base import ImageGenerator, ImageResult
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

                return ImageResult(data=image_bytes, status="completed")

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
