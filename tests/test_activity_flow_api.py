"""Complete P0 activity behavior through public HTTP contracts."""

import asyncio
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import func, select

from app.agents.base import ImageGenerator, ImageResult
from app.container import Container
from app.database import async_session_factory
from app.models.postcard import GeneratedImage
from tests.conftest import register_user


async def _start(client: AsyncClient, username: str = "闭环用户") -> tuple[str, dict[str, str]]:
    _, headers = await register_user(client, username)
    response = await client.post(
        "/api/activities", headers=headers, json={"mood": "想和大家画一幅温暖的画"}
    )
    assert response.status_code == 201
    return response.json()["room_id"], headers


async def test_fake_ai_completes_p0_flow_and_cleans_temporary_context(
    client: AsyncClient,
) -> None:
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
    assert sharing.json()["snapshot"]["stage"] == "AI_FOLLOW_UP"

    completed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={"command_id": "skip-1", "type": "skip_follow_up", "payload": {}},
    )
    snapshot = completed.json()["snapshot"]
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
        completed = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={"command_id": "skip-fail", "type": "skip_follow_up", "payload": {}},
        )
        postcard_id = completed.json()["snapshot"]["stage_state"]["postcard_id"]
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
