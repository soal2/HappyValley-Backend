"""Complete Painting House V2 behavior through public HTTP contracts."""

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


async def _start(
    client: AsyncClient, username: str = "闭环用户"
) -> tuple[str, dict[str, str], dict[str, object]]:
    user_id, headers = await register_user(client, username)
    response = await client.post(
        "/api/activities", headers=headers, json={"mood": "想和大家画一幅温暖的画"}
    )
    assert response.status_code == 201
    room_id = response.json()["room_id"]
    # 开场 + 前几个 AI 同伴发言在后台任务里跑(见 start_ai_intro_and_preroll);
    # 等它把房间推进到 PAINTING_TURN 轮到人类为止。
    snapshot: dict[str, object] = {}
    for _ in range(100):
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        if snapshot.get("stage") == "PAINTING_TURN":
            break
        await asyncio.sleep(0.05)
    assert snapshot.get("stage") == "PAINTING_TURN", (
        f"pre-roll did not reach PAINTING_TURN, got stage={snapshot.get('stage')!r}"
    )
    assert snapshot["stage_state"]["current_speaker_id"] == user_id
    return room_id, headers, snapshot


async def _submit_painting(
    client: AsyncClient,
    room_id: str,
    headers: dict[str, str],
    snapshot: dict[str, object],
    *,
    command_id: str,
    text: str,
):
    state = snapshot["stage_state"]
    assert isinstance(state, dict)
    return await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": command_id,
            "type": "submit_painting_turn",
            "payload": {"turn_index": state["painting_turn_index"], "text": text},
        },
    )


async def _complete_reflections(
    client: AsyncClient,
    room_id: str,
    headers: dict[str, str],
    reveal_snapshot: dict[str, object],
) -> dict[str, object]:
    revealed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "reveal-complete",
            "type": "complete_artwork_reveal",
            "payload": {"expected_revision": reveal_snapshot["revision"]},
        },
    )
    assert revealed.status_code == 200, revealed.text
    current = revealed.json()["snapshot"]
    for round_index in range(4):
        assert current["stage"] == "REFLECTION_TURN"
        response = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": f"reflection-{round_index}",
                "type": "submit_reflection",
                "payload": {
                    "round_index": round_index,
                    "text": f"这是我对第 {round_index + 1} 个问题的真实感受",
                },
            },
        )
        assert response.status_code == 200, response.text
        current = response.json()["snapshot"]
    return current


async def test_activity_starts_with_generated_facilitator_opening(client: AsyncClient) -> None:
    _, _, snapshot = await _start(client, "真实开场")

    opening = next(message for message in snapshot["messages"] if message["stage"] == "AI_INTRO")
    first_id = snapshot["stage_state"]["speaking_order"][0]
    first = next(
        member for member in snapshot["stage_state"]["members"] if member["id"] == first_id
    )
    assert opening["sender_id"] == "facilitator"
    assert first["name"] in opening["text"]
    assert opening["generation"]["source"] == "test"
    assert opening["generation"]["model"] == "fake-test-model"


async def test_fake_ai_completes_v2_with_27_text_calls_and_restorable_context(
    client: AsyncClient,
) -> None:
    runtime = Container.get().agent_runtime
    assert isinstance(runtime, FakeAgentRuntime)
    before = dict(runtime.calls)
    room_id, headers, initial = await _start(client)

    relay = await _submit_painting(
        client,
        room_id,
        headers,
        initial,
        command_id="painting-1",
        text="一棵长在山谷中央的小树",
    )
    assert relay.status_code == 200, relay.text
    reveal_snapshot = relay.json()["snapshot"]
    assert reveal_snapshot["stage"] == "ARTWORK_REVEAL"
    assert reveal_snapshot["stage_state"]["image_status"] == "completed"

    duplicate = await _submit_painting(
        client,
        room_id,
        headers,
        initial,
        command_id="painting-1",
        text="重复内容",
    )
    assert duplicate.json()["status"] == "already_processed"
    assert len(duplicate.json()["snapshot"]["messages"]) == len(reveal_snapshot["messages"])

    snapshot = await _complete_reflections(client, room_id, headers, reveal_snapshot)
    assert snapshot["stage"] == "COMPLETED"
    assert snapshot["messages"]
    assert len(snapshot["stage_state"]["reflection_rounds"]) == 4
    ai_messages = [message for message in snapshot["messages"] if message["sender_type"] == "ai"]
    assert len(ai_messages) == 26
    assert all(message["generation"] and message["speech"] for message in ai_messages)

    postcard_id = snapshot["stage_state"]["postcard_id"]
    postcard = await client.get(f"/api/postcards/{postcard_id}", headers=headers)
    assert postcard.status_code == 200
    assert postcard.json()["artwork"]["text_prompt"]
    assert len(postcard.json()["members"]) == 5
    assert postcard.json()["template_version"] == "painting-postcard-v2"
    assert [member["id"] for member in postcard.json()["members"]] == snapshot["stage_state"][
        "speaking_order"
    ]
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
    assert runtime.calls.get("reflection_question", 0) - before.get("reflection_question", 0) == 4
    assert runtime.calls.get("reflection_answer", 0) - before.get("reflection_answer", 0) == 16
    assert runtime.calls.get("summary", 0) - before.get("summary", 0) == 1
    assert runtime.calls.get("postcard_text", 0) - before.get("postcard_text", 0) == 1


class FailingImageGenerator(ImageGenerator):
    async def generate(self, prompt: str) -> ImageResult:
        return ImageResult(status="failed", error="simulated outage")


async def test_image_failure_stays_in_generation_until_real_retry(client: AsyncClient) -> None:
    room_id, headers, initial = await _start(client, "图片失败")
    container = Container.get()
    original = container.image_generator
    container.image_generator = FailingImageGenerator()
    try:
        failed = await _submit_painting(
            client,
            room_id,
            headers,
            initial,
            command_id="painting-fail",
            text="夜空下的一盏灯",
        )
        snapshot = failed.json()["snapshot"]
        assert snapshot["stage"] == "IMAGE_GENERATION"
        assert snapshot["stage_state"]["image_status"] == "failed"
        assert "postcard_id" not in snapshot["stage_state"]
    finally:
        container.image_generator = original

    retried = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={"command_id": "retry-image", "type": "retry_image", "payload": {}},
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["snapshot"]["stage"] == "ARTWORK_REVEAL"
    assert retried.json()["snapshot"]["stage_state"]["image_status"] == "completed"


async def test_concurrent_duplicate_command_generates_once(client: AsyncClient) -> None:
    room_id, headers, initial = await _start(client, "并发命令")
    state = initial["stage_state"]
    command = {
        "command_id": "same-command",
        "type": "submit_painting_turn",
        "payload": {
            "turn_index": state["painting_turn_index"],
            "text": "山谷里的一条小路",
        },
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
    assert len(snapshot["stage_state"]["contributions"]) == 5


async def test_event_delivery_failure_does_not_abort_room_orchestration(
    client: AsyncClient,
) -> None:
    room_id, headers, initial = await _start(client, "广播中断")
    user_id = (await client.get("/api/auth/me", headers=headers)).json()["user_id"]
    container = Container.get()

    async def disconnected_emitter(_: dict[str, object]) -> None:
        raise RuntimeError("socket already closed")

    async with async_session_factory() as session:
        result = await execute_command(
            room_id,
            user_id,
            "painting-after-disconnect",
            "submit_painting_turn",
            {
                "turn_index": initial["stage_state"]["painting_turn_index"],
                "text": "雨后森林里的一道浅浅彩虹",
            },
            session,
            container.agent_runtime,
            container.image_generator,
            container.media_storage,
            disconnected_emitter,
        )
    assert result == "accepted"
    snapshot = await client.get(f"/api/rooms/{room_id}", headers=headers)
    assert snapshot.json()["stage"] == "ARTWORK_REVEAL"
    assert len(snapshot.json()["stage_state"]["contributions"]) == 5


async def test_active_exit_removes_temporary_generated_media(client: AsyncClient) -> None:
    room_id, headers, initial = await _start(client, "退出清理")
    await _submit_painting(
        client,
        room_id,
        headers,
        initial,
        command_id="painting-exit",
        text="草地上的风筝",
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
