"""Real HTTP adapters validate protocols without paid external calls."""

import base64
import struct
import zlib
from types import SimpleNamespace

import httpx
import pytest

from app.agents.http_image import HttpImageGenerator
from app.agents.http_runtime import HttpAgentRuntime, _parse_json_object
from app.container import Container


def _valid_png(width: int = 64, height: int = 64) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    raw = b"".join(b"\x00" + (b"\x8c\xb8\x78" * width) for _ in range(height))

    def chunk(name: bytes, data: bytes) -> bytes:
        payload = name + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    return (
        signature
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "AI_API_KEY": "test-ai-key",
        "AI_MODEL": "configured-chat-model",
        "AI_BASE_URL": "https://models.example/v1",
        "AI_TIMEOUT_SECONDS": 3.0,
        "AI_NETWORK_MODE": "direct",
        "AI_PROXY_URL": None,
        "IMAGE_API_KEY": "test-image-key",
        "IMAGE_MODEL": "configured-image-model",
        "IMAGE_BASE_URL": "https://images.example/v1",
        "IMAGE_TIMEOUT_SECONDS": 4.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


async def test_chat_adapter_direct_mode_ignores_system_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '[{"character_id":"c1","rank":1,"reason":"合适"}]'}}
                ]
            },
        )

    def client_factory(**options: object) -> httpx.AsyncClient:
        if options.get("trust_env") is not False or options.get("proxy") is not None:
            raise httpx.ConnectError("错误地使用了系统代理")
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr("app.agents.http_runtime.get_settings", _settings)
    monkeypatch.setattr("app.agents.http_runtime.httpx.AsyncClient", client_factory)

    result = await HttpAgentRuntime(retry_delay_seconds=0).rank_candidates(
        "平静", 20, [{"id": "c1"}]
    )

    assert result[0].character_id == "c1"


async def test_chat_adapter_proxy_mode_uses_explicit_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    expected_proxy = "http://127.0.0.1:7890"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '[{"character_id":"c1","rank":1,"reason":"合适"}]'}}
                ]
            },
        )

    def client_factory(**options: object) -> httpx.AsyncClient:
        if options.get("trust_env") is not False or options.get("proxy") != expected_proxy:
            raise httpx.ConnectError("没有使用显式 AI 代理")
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        "app.agents.http_runtime.get_settings",
        lambda: _settings(AI_NETWORK_MODE="proxy", AI_PROXY_URL=expected_proxy),
    )
    monkeypatch.setattr("app.agents.http_runtime.httpx.AsyncClient", client_factory)

    result = await HttpAgentRuntime(retry_delay_seconds=0).rank_candidates(
        "平静", 20, [{"id": "c1"}]
    )

    assert result[0].character_id == "c1"


async def test_chat_adapter_auto_mode_falls_back_from_proxy_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.AsyncClient
    expected_proxy = "http://127.0.0.1:7890"
    routes: list[object] = []

    def proxy_failure(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("代理未启动")

    def direct_success(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '[{"character_id":"c1","rank":1,"reason":"直连成功"}]'}}
                ]
            },
        )

    def client_factory(**options: object) -> httpx.AsyncClient:
        route = options.get("proxy")
        routes.append(route)
        handler = proxy_failure if route == expected_proxy else direct_success
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(
        "app.agents.http_runtime.get_settings",
        lambda: _settings(AI_NETWORK_MODE="auto", AI_PROXY_URL=expected_proxy),
    )
    monkeypatch.setattr("app.agents.http_runtime.httpx.AsyncClient", client_factory)

    result = await HttpAgentRuntime(retry_delay_seconds=0).rank_candidates(
        "平静", 20, [{"id": "c1"}]
    )

    assert result[0].reason == "直连成功"
    assert routes == [expected_proxy, None]


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
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(_valid_png()).decode()}]},
        )

    monkeypatch.setattr("app.agents.http_image.get_settings", _settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = HttpImageGenerator(client, retry_delay_seconds=0)
        result = await generator.generate("山谷")
    assert attempts == 2
    assert result.status == "completed"
    assert result.data == _valid_png()
    assert result.generation is not None
    assert result.generation.source == "remote"
    assert result.generation.model == "configured-image-model"


async def test_all_visible_text_generators_return_remote_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        request_id = f"remote-{len(request_ids) + 1}"
        request_ids.append(request_id)
        if "温暖开场" in body:
            text = (
                "欢迎大家带着此刻的心情，一起接龙共同完成一幅画。"
                "结束后会获得专属明信片。现在请小林先来，用文字说说希望出现的画面。"
            )
        elif "文字接龙中的AI团友" in body:
            text = "我为原有画面增加一条穿过草地的温柔溪流，让暖光落在水面上。"
        elif "针对用户刚分享" in body:
            text = "我听见了你的安心，你能辨认并表达这份感受，展现了细腻的自我觉察。"
        elif "AI团体带领者小晴" in body:
            text = (
                '{"summary_text":"大家共同完成了一幅温暖的画。",'
                '"encouragement_text":"愿你记得这份温暖。"}'
            )
        else:
            text = "愿你保留此刻看见微光的能力，也温柔地陪伴自己。"
        return httpx.Response(
            200,
            json={"id": request_id, "choices": [{"message": {"content": text}}]},
        )

    monkeypatch.setattr("app.agents.http_runtime.get_settings", _settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runtime = HttpAgentRuntime(client, retry_delay_seconds=0)
        opening = await runtime.generate_opening([], [], {"id": "first", "name": "小林"})
        peer = await runtime.generate_peer_turn({"name": "团友"}, ["一棵树"])
        response = await runtime.generate_facilitator_response("共同画面", "我感到安心")
        summary = await runtime.generate_summary("共同画面", [])
        postcard = await runtime.generate_postcard_text("总结", "安心")

    metadata = [
        opening.generation,
        peer.generation,
        response.generation,
        summary.generation,
        postcard.generation,
    ]
    assert all(item is not None and item.source == "remote" for item in metadata)
    assert [item.request_id for item in metadata if item is not None] == request_ids


def test_opening_validation_requires_v2_flow_and_assigned_first_speaker() -> None:
    HttpAgentRuntime._validate_opening(
        "欢迎一起共同完成一幅画，结束后会获得明信片。现在请小林先来。",
        "小林",
    )

    with pytest.raises(ValueError, match="活动流程信息"):
        HttpAgentRuntime._validate_opening(
            "欢迎一起共同完成一幅画，结束后会获得明信片。现在请小周先来。",
            "小林",
        )


def test_model_json_object_accepts_markdown_fence_but_rejects_plain_text() -> None:
    assert _parse_json_object('```json\n{"text":"温暖"}\n```') == {"text": "温暖"}
    with pytest.raises(ValueError, match="结构化 JSON"):
        _parse_json_object("我感到温暖")


async def test_image_adapter_rejects_tiny_placeholder_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tiny = _valid_png(4, 4)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(tiny).decode()}]},
        )

    monkeypatch.setattr("app.agents.http_image.get_settings", _settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await HttpImageGenerator(client, retry_delay_seconds=0).generate("山谷")

    assert result.status == "failed"
    assert result.data is None


def test_container_rejects_invalid_backend_instead_of_loading_fake_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.container.get_settings", lambda: SimpleNamespace(AGENT_BACKEND="demo"))
    with pytest.raises(RuntimeError, match="AGENT_BACKEND 只允许 http 或 fake"):
        Container()
