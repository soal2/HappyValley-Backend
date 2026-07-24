"""Real HTTP adapters validate protocols without paid external calls."""

import base64
from types import SimpleNamespace

import httpx
import pytest

from app.agents.http_image import HttpImageGenerator
from app.agents.http_runtime import HttpAgentRuntime


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        AI_API_KEY="test-ai-key",
        AI_MODEL="configured-chat-model",
        AI_BASE_URL="https://models.example/v1",
        AI_TIMEOUT_SECONDS=3.0,
        IMAGE_API_KEY="test-image-key",
        IMAGE_MODEL="configured-image-model",
        IMAGE_BASE_URL="https://images.example/v1",
        IMAGE_TIMEOUT_SECONDS=4.0,
    )


async def test_chat_adapter_uses_configured_protocol_and_validates_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = request.read().decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '[{"character_id":"c1","rank":1,"reason":"合适"}]'}}
                ]
            },
        )

    monkeypatch.setattr("app.agents.http_runtime.get_settings", _settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = HttpAgentRuntime(client, retry_delay_seconds=0)
        result = await runtime.rank_candidates("平静", 20, [{"id": "c1"}])
    assert result[0].character_id == "c1"
    assert captured["authorization"] == "Bearer test-ai-key"
    assert "configured-chat-model" in str(captured["body"])


async def test_image_adapter_retries_once_and_returns_validated_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="temporary")
        assert "configured-image-model" in request.read().decode()
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"png").decode()}]})

    monkeypatch.setattr("app.agents.http_image.get_settings", _settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = HttpImageGenerator(client, retry_delay_seconds=0)
        result = await generator.generate("山谷")
    assert attempts == 2
    assert result.status == "completed"
    assert result.data == b"png"
