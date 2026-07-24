"""Complete P0 activity behavior through public HTTP contracts."""

import asyncio
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import func, select

from app.agents.base import ImageGenerator, ImageResult
from app.agents.fake_runtime import FakeAgentRuntime
from app.container import Container
from app.database import async_session_factory
from app.models.postcard import GeneratedImage
from app.services.activity_service import execute_command
from tests.conftest import register_user


async def _start(client: AsyncClient, username: str = "闭环用户") -> tuple[str, dict[str, str]]:
    _, headers = await register_user(client, username)
    response = await client.post(
        "/api/activities", headers=headers, json={"mood": "想和大家画一幅温暖的画"}
    )
    assert response.status_code == 201
    room_id = response.json()["room_id"]
    snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
    skipped = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": f"intro-{username}",
            "type": "skip_current_stage",
            "payload": {
                "expected_stage": snapshot["stage"],
                "expected_revision": snapshot["revision"],
            },
        },
    )
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["snapshot"]["stage"] == "HUMAN_BASE_PAINTING"
    return room_id, headers


async def test_activity_starts_with_generated_facilitator_opening(client: AsyncClient) -> None:
    _, headers = await register_user(client, "真实开场")
    started = await client.post(
        "/api/activities", headers=headers, json={"mood": "想和大家安静地画一会儿"}
    )
    snapshot = (await client.get(f"/api/rooms/{started.json()['room_id']}", headers=headers)).json()

    assert snapshot["stage"] == "AI_INTRO"
    opening = snapshot["messages"][-1]
    assert opening["sender_id"] == "facilitator"
    assert opening["generation"]["source"] == "test"
    assert opening["generation"]["model"] == "fake-test-model"


async def test_fake_ai_completes_p0_flow_and_cleans_temporary_context(
    client: AsyncClient,
) -> None:
    runtime = Container.get().agent_runtime
    assert isinstance(runtime, FakeAgentRuntime)
    before = dict(runtime.calls)
    room_id, headers = await _start(client)

    relay = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "base-1",
            "type": "submit_base",
            "payload": {"text": "一棵长在山谷中央的小树"},
        },
    )
    assert relay.status_code == 200
    assert relay.json()["snapshot"]["stage"] == "HUMAN_SHARING"
    assert relay.json()["snapshot"]["stage_state"]["image_status"] == "completed"
    ai_messages = [
        message
        for message in relay.json()["snapshot"]["messages"]
        if message["sender_type"] == "ai" and message["sender_id"] != "facilitator"
    ]
    assert len(ai_messages) == 4

    duplicate = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "base-1",
            "type": "submit_base",
            "payload": {"text": "重复内容"},
        },
    )
    assert duplicate.json()["status"] == "already_processed"
    assert len(duplicate.json()["snapshot"]["messages"]) == len(
        relay.json()["snapshot"]["messages"]
    )

    sharing = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "share-1",
            "type": "submit_sharing",
            "payload": {"text": "我看到小树时觉得自己也可以慢慢长大"},
        },
    )
    snapshot = sharing.json()["snapshot"]
    assert snapshot["stage"] == "COMPLETED"
    assert snapshot["messages"] == []
    assert set(snapshot["stage_state"]) <= {
        "activity_version",
        "cleanup_at",
        "duration_seconds",
        "final_stage",
        "postcard_id",
        "processed_command_ids",
        "template_version",
        "artwork_url",
        "members",
        "facilitator",
        "image_status",
        "generation_status",
        "current_speaker_id",
    }

    postcard_id = snapshot["stage_state"]["postcard_id"]
    postcard = await client.get(f"/api/postcards/{postcard_id}", headers=headers)
    assert postcard.status_code == 200
    assert postcard.json()["artwork"]["text_prompt"]
    assert len(postcard.json()["members"]) == 5
    assert postcard.json()["template_version"] == "painting-postcard-v1"
    async with async_session_factory() as session:
        uncleared = await session.scalar(
            select(func.count())
            .select_from(GeneratedImage)
            .where(
                GeneratedImage.room_id == room_id,
                GeneratedImage.prompt_history.is_not(None),
            )
        )
    assert uncleared == 0
    assert runtime.calls.get("opening", 0) - before.get("opening", 0) == 1
    assert runtime.calls.get("peer_turn", 0) - before.get("peer_turn", 0) == 4
    assert runtime.calls.get("facilitator_response", 0) - before.get("facilitator_response", 0) == 1
    assert runtime.calls.get("summary", 0) - before.get("summary", 0) == 1
    assert runtime.calls.get("postcard_text", 0) - before.get("postcard_text", 0) == 1


class FailingImageGenerator(ImageGenerator):
    async def generate(self, prompt: str) -> ImageResult:
        return ImageResult(status="failed", error="simulated outage")


async def test_image_failure_still_creates_text_postcard(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "图片失败")
    container = Container.get()
    original = container.image_generator
    container.image_generator = FailingImageGenerator()
    try:
        relay = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": "base-fail",
                "type": "submit_base",
                "payload": {"text": "夜空下的一盏灯"},
            },
        )
        assert relay.json()["snapshot"]["stage_state"]["image_status"] == "failed"
        await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": "share-fail",
                "type": "submit_sharing",
                "payload": {"text": "这盏灯让我安心"},
            },
        )
        completed = await client.get(f"/api/rooms/{room_id}", headers=headers)
        postcard_id = completed.json()["stage_state"]["postcard_id"]
        postcard = await client.get(f"/api/postcards/{postcard_id}", headers=headers)
        assert postcard.json()["artwork"]["image_url"] is None
        assert postcard.json()["artwork"]["text_prompt"]
    finally:
        container.image_generator = original


async def test_concurrent_duplicate_command_generates_once(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "并发命令")
    command = {
        "command_id": "same-command",
        "type": "submit_base",
        "payload": {"text": "山谷里的一条小路"},
    }

    async def submit() -> dict[str, object]:
        response = await client.post(
            f"/api/rooms/{room_id}/commands", headers=headers, json=command
        )
        assert response.status_code == 200, response.text
        return response.json()

    results = await asyncio.gather(submit(), submit())
    assert {str(result["status"]) for result in results} == {
        "accepted",
        "already_processed",
    }
    snapshot = results[0]["snapshot"]
    assert isinstance(snapshot, dict)
    ai_messages = [
        message
        for message in snapshot["messages"]
        if message["sender_type"] == "ai" and message["sender_id"] != "facilitator"
    ]
    assert len(ai_messages) == 4


async def test_event_delivery_failure_does_not_abort_room_orchestration(
    client: AsyncClient,
) -> None:
    room_id, headers = await _start(client, "广播中断")
    user_id = (await client.get("/api/auth/me", headers=headers)).json()["user_id"]
    container = Container.get()

    async def disconnected_emitter(_: dict[str, object]) -> None:
        raise RuntimeError("socket already closed")

    async with async_session_factory() as session:
        result = await execute_command(
            room_id,
            user_id,
            "base-after-disconnect",
            "submit_base",
            {"text": "雨后森林里的一道浅浅彩虹"},
            session,
            container.agent_runtime,
            container.image_generator,
            container.media_storage,
            disconnected_emitter,
        )
    assert result == "accepted"
    snapshot = await client.get(f"/api/rooms/{room_id}", headers=headers)
    assert snapshot.json()["stage"] == "HUMAN_SHARING"
    assert len(snapshot.json()["stage_state"]["contributions"]) == 5


async def test_active_exit_removes_temporary_generated_media(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "退出清理")
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "base-exit",
            "type": "submit_base",
            "payload": {"text": "草地上的风筝"},
        },
    )
    media_files = list((Path(__file__).parent / "media").rglob("*.png"))
    assert len(media_files) == 1
    exited = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={"command_id": "exit-after-image", "type": "exit", "payload": {}},
    )
    assert exited.json()["snapshot"]["stage"] == "TERMINATED"
    assert list((Path(__file__).parent / "media").rglob("*.png")) == []
