"""Edge TTS adapter — free Microsoft Neural Voices.

Uses the `edge-tts` package (no API key required) to synthesize natural Chinese
speech with per-character voice and prosody configuration, optimized for the
healing-room dialogue scenario.
"""

import hashlib
import io
import json
import time
from collections import OrderedDict
from dataclasses import dataclass

import edge_tts
import structlog

from app.config import get_settings
from app.services.tts_service import TtsAudio, TtsServiceError, mp3_duration_ms

logger = structlog.get_logger()


@dataclass(frozen=True)
class EdgeVoiceProfile:
    """Per-character voice configuration for Edge TTS."""

    voice: str  # Azure Neural Voice name
    rate: str  # Speaking rate adjustment, e.g. "-8%", "+5%"
    pitch: str  # Pitch adjustment, e.g. "+2Hz", "-5Hz"
    volume: str  # Volume adjustment, e.g. "+0%"


EDGE_VOICE_PROFILES: dict[str, EdgeVoiceProfile] = {
    "facilitator": EdgeVoiceProfile(
        voice="zh-CN-XiaoxiaoNeural",
        rate="-5%",
        pitch="+0Hz",
        volume="+0%",
    ),
    "you": EdgeVoiceProfile(
        voice="zh-CN-YunxiNeural",
        rate="+0%",
        pitch="+0Hz",
        volume="+0%",
    ),
    "zhu": EdgeVoiceProfile(
        voice="zh-CN-YunjianNeural",
        rate="-8%",
        pitch="-2Hz",
        volume="+0%",
    ),
    "xiaoli": EdgeVoiceProfile(
        voice="zh-CN-XiaoyiNeural",
        rate="+3%",
        pitch="+2Hz",
        volume="+0%",
    ),
    "zhang": EdgeVoiceProfile(
        voice="zh-CN-YunyangNeural",
        rate="-3%",
        pitch="-1Hz",
        volume="+0%",
    ),
    "li": EdgeVoiceProfile(
        voice="zh-CN-YunxiaNeural",
        rate="-6%",
        pitch="+2Hz",
        volume="+0%",
    ),
}


class EdgeTtsService:
    """Edge TTS service using Microsoft Neural Voices — no API key needed."""

    def __init__(self) -> None:
        settings = get_settings()
        self._cache_max_items = max(1, settings.TTS_CACHE_MAX_ITEMS)
        self._cache_ttl_seconds = max(1, settings.TTS_CACHE_TTL_SECONDS)
        self._cache: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    async def synthesize(
        self, *, request_id: str, text: str, speaker_id: str, language: str
    ) -> TtsAudio:
        profile = EDGE_VOICE_PROFILES.get(speaker_id)
        if profile is None:
            raise TtsServiceError(422, "TTS_UNKNOWN_SPEAKER", "不支持的语音角色")

        cache_key = self._cache_key(text, speaker_id, language, profile)
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.info(
                "tts.synthesize.completed",
                provider="edge",
                request_id=request_id,
                speaker_id=speaker_id,
                char_count=len(text),
                cache_hit=True,
            )
            return TtsAudio(
                content=cached,
                media_type="audio/mpeg",
                cache_hit=True,
                duration_ms=mp3_duration_ms(cached),
            )

        started = time.perf_counter()
        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=profile.voice,
                rate=profile.rate,
                pitch=profile.pitch,
                volume=profile.volume,
            )
            buffer = io.BytesIO()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buffer.write(chunk["data"])
            audio_bytes = buffer.getvalue()
        except Exception as error:
            self._log_failure(request_id, speaker_id, text, started, "synthesis_failed")
            raise TtsServiceError(
                502, "TTS_UPSTREAM_FAILED", f"Edge TTS 语音合成失败: {error}"
            ) from error

        if not audio_bytes:
            self._log_failure(request_id, speaker_id, text, started, "empty_audio")
            raise TtsServiceError(502, "TTS_INVALID_AUDIO", "Edge TTS 未返回有效音频")

        self._put_cached(cache_key, audio_bytes)
        audio_duration_ms = mp3_duration_ms(audio_bytes)
        logger.info(
            "tts.synthesize.completed",
            provider="edge",
            request_id=request_id,
            speaker_id=speaker_id,
            char_count=len(text),
            synthesis_latency_ms=round((time.perf_counter() - started) * 1000),
            audio_duration_ms=audio_duration_ms,
            cache_hit=False,
        )
        return TtsAudio(
            content=audio_bytes,
            media_type="audio/mpeg",
            cache_hit=False,
            duration_ms=audio_duration_ms,
        )

    def _cache_key(
        self, text: str, speaker_id: str, language: str, profile: EdgeVoiceProfile
    ) -> str:
        cache_input = json.dumps(
            {
                "provider": "edge",
                "speaker": speaker_id,
                "language": language,
                "voice": profile.voice,
                "rate": profile.rate,
                "pitch": profile.pitch,
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
            provider="edge",
            request_id=request_id,
            speaker_id=speaker_id,
            char_count=len(text),
            duration_ms=round((time.perf_counter() - started) * 1000),
            reason=reason,
        )
