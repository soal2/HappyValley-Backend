"""HTTP-based image generator using OpenAI DALL-E compatible API."""

import re
import struct
import uuid
import zlib

import httpx
import structlog

from app.agents.base import GenerationMeta, ImageGenerator, ImageResult
from app.config import get_settings

logger = structlog.get_logger()

# 只出「动画/治愈系插画」风：把用户拼接的画面意象包进统一的风格前后缀，杜绝写实照片/真人。
STYLE_PREFIX = (
    "治愈系动画插画风格，柔和水彩质感，温暖明亮的色调，吉卜力/绘本风，cinematic soft lighting，"
    "healing anime illustration, soft watercolor, warm palette, storybook aesthetic。"
    "画面主体内容："
)
STYLE_SUFFIX = "。整体必须是插画/动画风格，不要写实照片、不要真人。"

# 审核保底：净化时命中即删除的敏感片段（暴力/血腥/露骨/危险等类目的常见词）。
_UNSAFE_TERMS = (
    "血",
    "血腥",
    "暴力",
    "杀",
    "死",
    "尸",
    "自杀",
    "自残",
    "刀",
    "枪",
    "武器",
    "爆炸",
    "裸",
    "裸体",
    "色情",
    "性",
    "毒",
    "毒品",
    "恐怖",
    "仇恨",
    "歧视",
    "虐待",
    "战争",
)
# 净化后若为空时的通用安全意象。
_SAFE_FALLBACK_PROMPT = "温暖治愈的自然风景，阳光、花草、柔和的天空"


def _stylize(prompt: str) -> str:
    """套用治愈系动画风格前后缀。"""
    return f"{STYLE_PREFIX}{prompt}{STYLE_SUFFIX}"


def _sanitize(prompt: str) -> str:
    """净化用户意象：删掉敏感片段与非常规字符，只留温和的自然意象；空则回退安全意象。"""
    cleaned = prompt
    for term in _UNSAFE_TERMS:
        cleaned = cleaned.replace(term, "")
    # 只保留中日文、英文字母、数字与常见温和标点，其余（含 emoji/特殊符号）一律去除。
    cleaned = re.sub(r"[^一-鿿぀-ヿA-Za-z0-9，。、,.\s]", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned or _SAFE_FALLBACK_PROMPT


def _fallback_artwork_png() -> bytes:
    """兜底安全图：1024×1024 柔和暖色竖向渐变 PNG（纯 Python 生成，永不依赖外部）。"""
    width = height = 1024
    raw = bytearray()
    for y in range(height):
        t = y / (height - 1)
        # 顶部暖桃色 → 底部柔黄绿，营造治愈系晨光基调。
        r = int(0xF6 + (0xEA - 0xF6) * t)
        g = int(0xD9 + (0xF0 - 0xD9) * t)
        b = int(0xC0 + (0xC8 - 0xC0) * t)
        raw.append(0)  # 每行 filter byte = 0
        raw.extend(bytes((r, g, b)) * width)

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        body = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + body + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit, color type 2 (RGB)
    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", ihdr)
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    return png


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
        """三级降级出图，保证活动一定拿到可展示的插画：

        1. 原始意象 + 治愈系动画风格，正常调用（含 1 次退避重试）。
        2. 上一步失败（含审核拒绝/返回非图/网络错误）→ 净化关键词后再调一次。
        3. 仍失败 → 返回兜底安全图（status=completed），绝不让绘画疗愈卡在失败态。
        """
        # 第一级：原始意象。
        result = await self._call_api(_stylize(prompt))
        if result.status == "completed":
            return result

        # 第二级：净化关键词重试（覆盖审核风险）。
        sanitized = _sanitize(prompt)
        logger.warning(
            "http_image.sanitized_retry",
            reason=result.error,
            sanitized_preview=sanitized[:50],
        )
        result = await self._call_api(_stylize(sanitized))
        if result.status == "completed":
            return result

        # 第三级：兜底安全图，保证流程走完。
        logger.warning("http_image.fallback_image_used", reason=result.error)
        return ImageResult(
            data=_fallback_artwork_png(),
            status="completed",
            generation=GenerationMeta(
                source="fallback",
                model=self._model,
                request_id=str(uuid.uuid4()),
            ),
        )

    async def _call_api(self, styled_prompt: str) -> ImageResult:
        """调一次图片 API（含 1 次退避重试）。失败返回 status=failed，不抛异常。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "prompt": styled_prompt,
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
