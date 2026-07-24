"""ElevenLabs TTS adapter with server-owned character voices and ephemeral caching."""

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger()
VOICE_PROFILE_VERSION = "elevenlabs-multilingual-v1"


class TtsSettings(Protocol):
    TTS_PROVIDER: str
    ELEVENLABS_API_KEY: str | None
    ELEVENLABS_TTS_MODEL: str
    ELEVENLABS_TTS_BASE_URL: str
    ELEVENLABS_TTS_TIMEOUT_SECONDS: float
    ELEVENLABS_TTS_OUTPUT_FORMAT: str
    ELEVENLABS_VOICE_FACILITATOR_ID: str | None
    ELEVENLABS_VOICE_YOU_ID: str | None
    ELEVENLABS_VOICE_ZHU_ID: str | None
    ELEVENLABS_VOICE_XIAOLI_ID: str | None
    ELEVENLABS_VOICE_ZHANG_ID: str | None
    ELEVENLABS_VOICE_LI_ID: str | None
    TTS_CACHE_MAX_ITEMS: int
    TTS_CACHE_TTL_SECONDS: int


@dataclass(frozen=True)
class VoiceProfile:
    stability: float
    similarity_boost: float
    style: float
    speed: float
    use_speaker_boost: bool = True


@dataclass(frozen=True)
class TtsAudio:
    content: bytes
    media_type: str
    cache_hit: bool


class TtsServiceError(Exception):
    def __init__(self, status_code: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail


VOICE_PROFILES: dict[str, VoiceProfile] = {
    "facilitator": VoiceProfile(
        stability=0.72,
        similarity_boost=0.75,
        style=0.12,
        speed=0.92,
    ),
    "zhu": VoiceProfile(
        stability=0.68,
        similarity_boost=0.75,
        style=0.18,
        speed=0.90,
    ),
    "xiaoli": VoiceProfile(
        stability=0.52,
        similarity_boost=0.72,
        style=0.28,
        speed=1.03,
    ),
    "zhang": VoiceProfile(
        stability=0.78,
        similarity_boost=0.74,
        style=0.08,
        speed=0.94,
    ),
    "li": VoiceProfile(
        stability=0.65,
        similarity_boost=0.76,
        style=0.20,
        speed=0.90,
    ),
    "you": VoiceProfile(
        stability=0.72,
        similarity_boost=0.72,
        style=0.05,
        speed=1.0,
    ),
}


class ElevenLabsTtsService:
    def __init__(self, settings: TtsSettings, client: httpx.AsyncClient | None = None) -> None:
        if settings.TTS_PROVIDER.lower() != "elevenlabs":
            raise TtsServiceError(503, "TTS_NOT_CONFIGURED", "ElevenLabs TTS 未启用")
        if not settings.ELEVENLABS_API_KEY:
            raise TtsServiceError(503, "TTS_NOT_CONFIGURED", "ElevenLabs API Key 尚未配置")
        if not settings.ELEVENLABS_TTS_OUTPUT_FORMAT.startswith("mp3_"):
            raise TtsServiceError(503, "TTS_INVALID_CONFIG", "当前仅支持 ElevenLabs MP3 输出")

        self._api_key = settings.ELEVENLABS_API_KEY
        self._model = settings.ELEVENLABS_TTS_MODEL
        self._base_url = settings.ELEVENLABS_TTS_BASE_URL.rstrip("/")
        self._timeout = settings.ELEVENLABS_TTS_TIMEOUT_SECONDS
        self._output_format = settings.ELEVENLABS_TTS_OUTPUT_FORMAT
        self._voice_ids = {
            "facilitator": settings.ELEVENLABS_VOICE_FACILITATOR_ID,
            "you": settings.ELEVENLABS_VOICE_YOU_ID,
            "zhu": settings.ELEVENLABS_VOICE_ZHU_ID,
            "xiaoli": settings.ELEVENLABS_VOICE_XIAOLI_ID,
            "zhang": settings.ELEVENLABS_VOICE_ZHANG_ID,
            "li": settings.ELEVENLABS_VOICE_LI_ID,
        }
        self._cache_max_items = max(1, settings.TTS_CACHE_MAX_ITEMS)
        self._cache_ttl_seconds = max(1, settings.TTS_CACHE_TTL_SECONDS)
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self._client = client

    async def synthesize(
        self, *, request_id: str, text: str, speaker_id: str, language: str
    ) -> TtsAudio:
        profile = VOICE_PROFILES.get(speaker_id)
        if profile is None:
            raise TtsServiceError(422, "TTS_UNKNOWN_SPEAKER", "不支持的语音角色")
        voice_id = self._voice_ids.get(speaker_id)
        if not voice_id:
            raise TtsServiceError(
                503,
                "TTS_VOICE_NOT_CONFIGURED",
                "该角色的 ElevenLabs Voice ID 尚未配置",
            )

        cache_key = self._cache_key(text, speaker_id, language, voice_id, profile)
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.info(
                "tts.synthesize.completed",
                provider="elevenlabs",
                request_id=request_id,
                speaker_id=speaker_id,
                model=self._model,
                char_count=len(text),
                cache_hit=True,
            )
            return TtsAudio(content=cached, media_type="audio/mpeg", cache_hit=True)

        payload: dict[str, object] = {
            "text": text,
            "model_id": self._model,
            "voice_settings": asdict(profile),
        }
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        started = time.perf_counter()
        try:
            if self._client is not None:
                response = await self._client.post(
                    f"{self._base_url}/text-to-speech/{voice_id}",
                    params={"output_format": self._output_format},
                    json=payload,
                    headers=headers,
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        f"{self._base_url}/text-to-speech/{voice_id}",
                        params={"output_format": self._output_format},
                        json=payload,
                        headers=headers,
                    )
        except httpx.TimeoutException as error:
            self._log_failure(request_id, speaker_id, text, started, "timeout")
            raise TtsServiceError(504, "TTS_TIMEOUT", "ElevenLabs 语音生成超时") from error
        except httpx.HTTPError as error:
            self._log_failure(request_id, speaker_id, text, started, "network")
            raise TtsServiceError(
                502, "TTS_UPSTREAM_FAILED", "无法连接 ElevenLabs 语音服务"
            ) from error

        if response.status_code == 402:
            self._log_failure(request_id, speaker_id, text, started, "quota_exhausted")
            raise TtsServiceError(402, "TTS_QUOTA_EXHAUSTED", "ElevenLabs 语音额度不足")
        if response.status_code == 429:
            self._log_failure(request_id, speaker_id, text, started, "rate_limited")
            raise TtsServiceError(429, "TTS_RATE_LIMITED", "ElevenLabs 语音服务繁忙，请稍后再试")
        if response.status_code in {401, 403}:
            self._log_failure(request_id, speaker_id, text, started, "upstream_auth")
            raise TtsServiceError(
                502,
                "TTS_UPSTREAM_AUTH_FAILED",
                "ElevenLabs 语音服务鉴权或权限配置错误",
            )
        if response.status_code >= 400:
            self._log_failure(request_id, speaker_id, text, started, "upstream")
            raise TtsServiceError(502, "TTS_UPSTREAM_FAILED", "ElevenLabs 语音生成失败")

        media_type = response.headers.get("Content-Type", "").split(";", maxsplit=1)[0]
        if not response.content or not media_type.startswith("audio/"):
            self._log_failure(request_id, speaker_id, text, started, "invalid_audio")
            raise TtsServiceError(502, "TTS_INVALID_AUDIO", "ElevenLabs 未返回有效音频")

        self._put_cached(cache_key, response.content)
        logger.info(
            "tts.synthesize.completed",
            provider="elevenlabs",
            request_id=request_id,
            speaker_id=speaker_id,
            model=self._model,
            char_count=len(text),
            duration_ms=round((time.perf_counter() - started) * 1000),
            cache_hit=False,
        )
        return TtsAudio(content=response.content, media_type=media_type, cache_hit=False)

    def _cache_key(
        self,
        text: str,
        speaker_id: str,
        language: str,
        voice_id: str,
        profile: VoiceProfile,
    ) -> str:
        cache_input = json.dumps(
            {
                "version": VOICE_PROFILE_VERSION,
                "model": self._model,
                "format": self._output_format,
                "speaker": speaker_id,
                "language": language,
                "voice_id": voice_id,
                "voice_settings": asdict(profile),
                "text": text,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(cache_input.encode("utf-8")).hexdigest()

    def _get_cached(self, key: str) -> bytes | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, content = entry
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return content

    def _put_cached(self, key: str, content: bytes) -> None:
        self._cache[key] = (time.monotonic() + self._cache_ttl_seconds, content)
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max_items:
            self._cache.popitem(last=False)

    def _log_failure(
        self, request_id: str, speaker_id: str, text: str, started: float, reason: str
    ) -> None:
        logger.warning(
            "tts.synthesize.failed",
            provider="elevenlabs",
            request_id=request_id,
            speaker_id=speaker_id,
            model=self._model,
            char_count=len(text),
            duration_ms=round((time.perf_counter() - started) * 1000),
            reason=reason,
        )


_tts_service: ElevenLabsTtsService | None = None


def get_tts_service() -> ElevenLabsTtsService:
    global _tts_service
    if _tts_service is None:
        _tts_service = ElevenLabsTtsService(get_settings())
    return _tts_service


def reset_tts_service() -> None:
    global _tts_service
    _tts_service = None
