"""Painting relay V2 behavior through public HTTP contracts."""

from httpx import AsyncClient

from app.agents.base import ReflectionAnswerResult
from app.agents.fake_runtime import FakeAgentRuntime
from app.container import Container
from app.database import async_session_factory
from app.models.room import ActivityRoom
from app.services.activity_service import _valid_reflection_question
from app.services.speech_service import split_speech
from tests.conftest import register_user


def test_speech_chunks_do_not_leave_a_punctuation_only_tail() -> None:
    text = "这画里的安稳气，像抗战时在李庄见的徽州老宅，暖得人想驻足。"
    chunks = split_speech(text, capacity=28)

    assert "".join(chunks) == text
    assert all(chunk.strip("。！？；!?，、：:…") for chunk in chunks)


def test_reflection_question_validation_accepts_natural_v2_intent_phrasing() -> None:
    assert _valid_reflection_question(1)("画下那一笔时，你心里浮现了什么？")
    assert _valid_reflection_question(2)("此刻你想告诉其他成员什么？")
    assert not _valid_reflection_question(3)("你为什么这样画？")


async def test_v2_room_persists_a_random_order_for_all_five_members(
    client: AsyncClient,
) -> None:
    _, headers = await register_user(client, "随机顺序用户")
    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "带着一点期待来和大家画画"},
    )

    assert started.status_code == 201, started.text
    snapshot = (
        await client.get(
            f"/api/rooms/{started.json()['room_id']}",
            headers=headers,
        )
    ).json()
    state = snapshot["stage_state"]
    member_ids = [member["id"] for member in state["members"]]

    assert state["activity_version"] == "2.0"
    assert len(state["speaking_order"]) == 5
    assert len(set(state["speaking_order"])) == 5
    assert set(state["speaking_order"]) == set(member_ids)
    assert "facilitator" not in state["speaking_order"]

    opening = next(message for message in snapshot["messages"] if message["stage"] == "AI_INTRO")
    assert opening["speech"]["voice_id"] == "facilitator"
    assert opening["speech"]["limit_ms"] == 20_000
    assert "".join(chunk["text"] for chunk in opening["speech"]["chunks"]) == opening["text"]


async def test_active_v1_room_is_safely_terminated_for_flow_upgrade(
    client: AsyncClient,
) -> None:
    user_id, headers = await register_user(client, "旧流程用户")
    async with async_session_factory() as session:
        legacy = ActivityRoom(
            user_id=user_id,
            status="active",
            current_stage="HUMAN_SHARING",
            stage_state={"activity_version": "1.0"},
        )
        session.add(legacy)
        await session.commit()
        room_id = legacy.id

    response = await client.get("/api/rooms/active", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "flow_upgraded"

    async with async_session_factory() as session:
        room = await session.get(ActivityRoom, room_id)
        assert room is not None
        assert room.status == "terminated"
        assert room.current_stage == "TERMINATED"
        assert room.stage_state["termination_reason"] == "flow_upgraded"


async def test_painting_order_runs_ai_turns_and_waits_for_the_human(
    client: AsyncClient,
) -> None:
    user_id, headers = await register_user(client, "随机绘画用户")
    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "想画一点明亮的颜色"},
    )
    room_id = started.json()["room_id"]
    snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
    state = snapshot["stage_state"]
    human_index = state["speaking_order"].index(user_id)

    assert snapshot["stage"] == "PAINTING_TURN"
    assert state["painting_turn_index"] == human_index
    assert state["current_speaker_id"] == user_id
    assert len(state["contributions"]) == human_index

    submitted = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "painting-human-turn",
            "type": "submit_painting_turn",
            "payload": {
                "turn_index": human_index,
                "text": "一条穿过春日草地的清澈小溪",
            },
        },
    )

    assert submitted.status_code == 200, submitted.text
    completed = submitted.json()["snapshot"]
    assert completed["stage"] == "ARTWORK_REVEAL"
    assert len(completed["stage_state"]["contributions"]) == 5
    assert [item["member_id"] for item in completed["stage_state"]["contributions"]] == state[
        "speaking_order"
    ]


async def test_four_reflection_rounds_reuse_order_and_allow_human_skip(
    client: AsyncClient,
) -> None:
    runtime = Container.get().agent_runtime
    assert isinstance(runtime, FakeAgentRuntime)
    before = dict(runtime.calls)
    user_id, headers = await register_user(client, "四轮回顾用户")
    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "想听听不同的看法"},
    )
    room_id = started.json()["room_id"]
    snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
    state = snapshot["stage_state"]
    human_index = state["speaking_order"].index(user_id)
    painted = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "paint-before-reflection",
            "type": "submit_painting_turn",
            "payload": {"turn_index": human_index, "text": "湖边围着五棵安静的小树"},
        },
    )
    assert painted.json()["snapshot"]["stage"] == "ARTWORK_REVEAL"

    revealed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "reveal-complete",
            "type": "complete_artwork_reveal",
            "payload": {"expected_revision": painted.json()["snapshot"]["revision"]},
        },
    )
    assert revealed.status_code == 200, revealed.text
    current = revealed.json()["snapshot"]

    for round_index in range(4):
        assert current["stage"] == "REFLECTION_TURN"
        assert current["stage_state"]["reflection_round_index"] == round_index
        assert current["stage_state"]["reflection_turn_index"] == human_index
        assert current["stage_state"]["current_speaker_id"] == user_id
        command_type = "skip_reflection" if round_index == 1 else "submit_reflection"
        payload: dict[str, object] = {"round_index": round_index}
        if command_type == "submit_reflection":
            payload["text"] = f"这是我对第 {round_index + 1} 个问题的真实感受"
        response = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": f"reflection-human-{round_index}",
                "type": command_type,
                "payload": payload,
            },
        )
        assert response.status_code == 200, response.text
        current = response.json()["snapshot"]

    assert current["stage"] == "COMPLETED"
    assert runtime.calls["reflection_question"] - before.get("reflection_question", 0) == 4
    assert runtime.calls["reflection_answer"] - before.get("reflection_answer", 0) == 16
    postcard = await client.get(
        f"/api/postcards/{current['stage_state']['postcard_id']}", headers=headers
    )
    assert postcard.status_code == 200
    assert [member["id"] for member in postcard.json()["members"]] == state["speaking_order"]


class InvalidReferenceFakeRuntime(FakeAgentRuntime):
    """Fake runtime that returns invalid message references to test filtering."""

    async def generate_reflection_answer(
        self,
        character: dict[str, object],
        question: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> ReflectionAnswerResult:
        result = await super().generate_reflection_answer(
            character, question, context, transcript, max_duration_seconds
        )
        return ReflectionAnswerResult(
            text=result.text,
            positive_feedback_type=result.positive_feedback_type,
            referenced_message_ids=["nonexistent-message-id-99999"],
            generation=result.generation,
        )


async def test_skip_reflection_with_ai_invalid_reference_does_not_crash(
    client: AsyncClient,
) -> None:
    """When the human skips a reflection round and an AI returns an invalid
    message reference, the activity should filter it out and continue
    instead of crashing with 'AI 团友引用了尚未发生的发言'."""
    container = Container.get()
    original_runtime = container.agent_runtime
    container.agent_runtime = InvalidReferenceFakeRuntime()
    try:
        user_id, headers = await register_user(client, "无效引用跳过测试用户")
        started = await client.post(
            "/api/activities",
            headers=headers,
            json={"mood": "带着一点期待来和大家画画"},
        )
        assert started.status_code == 201, started.text
        room_id = started.json()["room_id"]
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        state = snapshot["stage_state"]
        human_index = state["speaking_order"].index(user_id)
        painted = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": "paint-before-reflection",
                "type": "submit_painting_turn",
                "payload": {"turn_index": human_index, "text": "湖边围着五棵安静的小树"},
            },
        )
        assert painted.json()["snapshot"]["stage"] == "ARTWORK_REVEAL"

        revealed = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": "reveal-complete",
                "type": "complete_artwork_reveal",
                "payload": {"expected_revision": painted.json()["snapshot"]["revision"]},
            },
        )
        assert revealed.status_code == 200, revealed.text
        current = revealed.json()["snapshot"]

        for round_index in range(4):
            assert current["stage"] == "REFLECTION_TURN"
            assert current["stage_state"]["reflection_round_index"] == round_index
            assert current["stage_state"]["reflection_turn_index"] == human_index
            assert current["stage_state"]["current_speaker_id"] == user_id
            command_type = "skip_reflection" if round_index == 1 else "submit_reflection"
            payload: dict[str, object] = {"round_index": round_index}
            if command_type == "submit_reflection":
                payload["text"] = f"这是我对第 {round_index + 1} 个问题的真实感受"
            response = await client.post(
                f"/api/rooms/{room_id}/commands",
                headers=headers,
                json={
                    "command_id": f"reflection-human-{round_index}",
                    "type": command_type,
                    "payload": payload,
                },
            )
            assert response.status_code == 200, response.text
            current = response.json()["snapshot"]

        assert current["stage"] == "COMPLETED"
    finally:
        container.agent_runtime = original_runtime
