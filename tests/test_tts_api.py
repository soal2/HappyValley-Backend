"""ElevenLabs TTS proxy contracts, security, caching, and upstream failures."""

import json
from types import SimpleNamespace

import httpx
import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.config import Settings
from app.services.tts_service import (
    VOICE_PROFILES,
    ElevenLabsTtsService,
    TtsAudio,
    TtsServiceError,
)
from tests.conftest import register_user


def _tts_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "TTS_PROVIDER": "elevenlabs",
        "ELEVENLABS_API_KEY": "test-elevenlabs-key",
        "ELEVENLABS_TTS_MODEL": "eleven_multilingual_v2",
        "ELEVENLABS_TTS_BASE_URL": "https://api.elevenlabs.io/v1",
        "ELEVENLABS_TTS_TIMEOUT_SECONDS": 2.0,
        "ELEVENLABS_TTS_OUTPUT_FORMAT": "mp3_44100_128",
        "ELEVENLABS_VOICE_FACILITATOR_ID": "voice-facilitator",
        "ELEVENLABS_VOICE_YOU_ID": "voice-you",
        "ELEVENLABS_VOICE_ZHU_ID": "voice-zhu",
        "ELEVENLABS_VOICE_XIAOLI_ID": "voice-xiaoli",
        "ELEVENLABS_VOICE_ZHANG_ID": "voice-zhang",
        "ELEVENLABS_VOICE_LI_ID": "voice-li",
        "TTS_CACHE_MAX_ITEMS": 128,
        "TTS_CACHE_TTL_SECONDS": 600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_voice_profiles_cover_every_healing_room_speaker() -> None:
    assert set(VOICE_PROFILES) == {"facilitator", "you", "zhu", "xiaoli", "zhang", "li"}
    assert VOICE_PROFILES["facilitator"].speed < 1
    assert VOICE_PROFILES["xiaoli"].style > VOICE_PROFILES["zhang"].style
    assert VOICE_PROFILES["li"].speed < VOICE_PROFILES["you"].speed


async def test_elevenlabs_tts_uses_server_profile_and_caches_identical_audio() -> None:
    attempts = 0
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        captured["api_key"] = request.headers["xi-api-key"]
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.read())
        return httpx.Response(200, content=b"mp3-audio", headers={"Content-Type": "audio/mpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ElevenLabsTtsService(_tts_settings(), client=client)
        first = await service.synthesize(
            request_id="message-1",
            text="欢迎来到疗愈小屋。",
            speaker_id="facilitator",
            language="zh-CN",
        )
        second = await service.synthesize(
            request_id="message-2",
            text="欢迎来到疗愈小屋。",
            speaker_id="facilitator",
            language="zh-CN",
        )

    assert attempts == 1
    assert first.content == second.content == b"mp3-audio"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert captured["api_key"] == "test-elevenlabs-key"
    assert captured["url"] == (
        "https://api.elevenlabs.io/v1/text-to-speech/voice-facilitator?output_format=mp3_44100_128"
    )
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["model_id"] == "eleven_multilingual_v2"
    assert payload["text"] == "欢迎来到疗愈小屋。"
    assert payload["voice_settings"]["speed"] < 1
    assert "voice" not in payload


@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "expected_code"),
    [
        (402, 402, "TTS_QUOTA_EXHAUSTED"),
        (429, 429, "TTS_RATE_LIMITED"),
        (500, 502, "TTS_UPSTREAM_FAILED"),
    ],
)
async def test_elevenlabs_tts_maps_upstream_failures(
    upstream_status: int, expected_status: int, expected_code: str
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(upstream_status, text="upstream detail must not leak")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ElevenLabsTtsService(_tts_settings(), client=client)
        with pytest.raises(TtsServiceError) as raised:
            await service.synthesize(
                request_id="message-error",
                text="测试",
                speaker_id="you",
                language="zh-CN",
            )

    assert raised.value.status_code == expected_status
    assert raised.value.code == expected_code
    assert "upstream detail" not in raised.value.detail
    assert "test-elevenlabs-key" not in raised.value.detail


async def test_elevenlabs_tts_maps_timeout() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow upstream")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = ElevenLabsTtsService(_tts_settings(), client=client)
        with pytest.raises(TtsServiceError) as raised:
            await service.synthesize(
                request_id="message-timeout",
                text="测试",
                speaker_id="you",
                language="zh-CN",
            )

    assert raised.value.status_code == 504
    assert raised.value.code == "TTS_TIMEOUT"


async def test_elevenlabs_tts_requires_voice_id_for_requested_speaker() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        service = ElevenLabsTtsService(_tts_settings(ELEVENLABS_VOICE_ZHU_ID=None), client=client)
        with pytest.raises(TtsServiceError) as raised:
            await service.synthesize(
                request_id="message-no-voice",
                text="测试",
                speaker_id="zhu",
                language="zh-CN",
            )

    assert raised.value.status_code == 503
    assert raised.value.code == "TTS_VOICE_NOT_CONFIGURED"


def test_production_rejects_anonymous_tts_configuration() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MODE="production", TTS_ALLOW_ANONYMOUS_DEMO=True)


async def test_tts_route_requires_jwt_by_default(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routes.tts.get_settings",
        lambda: SimpleNamespace(MODE="demo", TTS_ALLOW_ANONYMOUS_DEMO=False),
    )
    response = await client.post(
        "/api/tts/synthesize",
        json={
            "requestId": "message-1",
            "text": "欢迎",
            "speakerId": "facilitator",
            "language": "zh-CN",
        },
    )
    assert response.status_code == 401


async def test_tts_route_returns_audio_for_authenticated_user(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, headers = await register_user(client, username="语音用户")
    calls: list[dict[str, str]] = []

    class FakeTtsService:
        async def synthesize(self, **request: str) -> TtsAudio:
            calls.append(request)
            return TtsAudio(content=b"audio", media_type="audio/mpeg", cache_hit=False)

    monkeypatch.setattr("app.routes.tts.get_tts_service", lambda: FakeTtsService())
    response = await client.post(
        "/api/tts/synthesize",
        headers=headers,
        json={
            "requestId": "message-2",
            "text": "今晚，从一幅画开始。",
            "speakerId": "facilitator",
            "language": "zh-CN",
        },
    )

    assert response.status_code == 200
    assert response.content == b"audio"
    assert response.headers["content-type"] == "audio/mpeg"
    assert calls[0]["speaker_id"] == "facilitator"


async def test_tts_route_allows_explicit_anonymous_demo(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeTtsService:
        async def synthesize(self, **_: str) -> TtsAudio:
            return TtsAudio(content=b"demo", media_type="audio/mpeg", cache_hit=False)

    monkeypatch.setattr(
        "app.routes.tts.get_settings",
        lambda: SimpleNamespace(MODE="demo", TTS_ALLOW_ANONYMOUS_DEMO=True),
    )
    monkeypatch.setattr("app.routes.tts.get_tts_service", lambda: FakeTtsService())
    response = await client.post(
        "/api/tts/synthesize",
        json={
            "requestId": "message-demo",
            "text": "测试",
            "speakerId": "you",
            "language": "zh-CN",
        },
    )
    assert response.status_code == 200


async def test_tts_route_rejects_unknown_speaker_and_long_text(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.routes.tts.get_settings",
        lambda: SimpleNamespace(MODE="demo", TTS_ALLOW_ANONYMOUS_DEMO=True),
    )
    unknown = await client.post(
        "/api/tts/synthesize",
        json={
            "requestId": "message-invalid",
            "text": "测试",
            "speakerId": "unknown",
            "language": "zh-CN",
        },
    )
    too_long = await client.post(
        "/api/tts/synthesize",
        json={
            "requestId": "message-long",
            "text": "字" * 1001,
            "speakerId": "you",
            "language": "zh-CN",
        },
    )
    blank = await client.post(
        "/api/tts/synthesize",
        json={
            "requestId": "message-blank",
            "text": "   ",
            "speakerId": "you",
            "language": "zh-CN",
        },
    )
    assert unknown.status_code == 422
    assert too_long.status_code == 422
    assert blank.status_code == 422
