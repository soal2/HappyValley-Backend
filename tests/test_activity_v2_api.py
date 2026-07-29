"""Painting relay V2 behavior through public HTTP contracts."""

import asyncio

from httpx import AsyncClient
from sqlalchemy import func, select, update

from app.agents.base import ReflectionAnswerResult
from app.agents.fake_runtime import FakeAgentRuntime
from app.container import Container
from app.database import async_session_factory
from app.models.room import ActivityRoom
from app.models.room_character import RoomCharacter
from app.services import activity_service, character_generation_service, room_service
from app.services.activity_service import _valid_reflection_question
from app.services.speech_service import split_speech
from tests.conftest import register_user


async def _wait_for_painting_turn(
    client: AsyncClient, room_id: str, headers: dict[str, str]
) -> dict[str, object]:
    """Poll snapshot until the background pre-roll task reaches PAINTING_TURN."""
    for _ in range(100):
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        if snapshot.get("stage") == "PAINTING_TURN":
            current_id = snapshot["stage_state"].get("current_speaker_id")
            current = next(
                (
                    member
                    for member in snapshot["stage_state"].get("members", [])
                    if member["id"] == current_id
                ),
                None,
            )
            if current and current["type"] == "human":
                return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"pre-roll did not reach PAINTING_TURN, last stage={snapshot.get('stage')!r}"  # noqa: F821
    )


async def _wait_for_human_or_completed(
    client: AsyncClient, room_id: str, headers: dict[str, str], round_index: int
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for _ in range(200):
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        if snapshot.get("stage") == "COMPLETED":
            return snapshot
        state = snapshot.get("stage_state", {})
        current_id = state.get("current_speaker_id")
        current = next(
            (member for member in state.get("members", []) if member["id"] == current_id),
            None,
        )
        if (
            snapshot.get("stage") == "REFLECTION_TURN"
            and state.get("reflection_round_index") == round_index
            and current
            and current["type"] == "human"
        ):
            return snapshot
        await asyncio.sleep(0.02)
    raise AssertionError(f"reflection did not advance: {snapshot}")


async def _wait_for_stage(
    client: AsyncClient, room_id: str, headers: dict[str, str], stage: str
) -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for _ in range(200):
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        if snapshot.get("stage") == stage:
            return snapshot
        await asyncio.sleep(0.02)
    raise AssertionError(f"room did not reach {stage}: {snapshot}")


async def test_room_snapshot_refreshes_member_display_fields_after_background_commit(
    client: AsyncClient,
) -> None:
    user_id, _ = await register_user(client, "成员校准用户")
    async with async_session_factory() as stale_session:
        room = ActivityRoom(
            user_id=user_id,
            status="active",
            current_stage="PAINTING_TURN",
            stage_state={
                "activity_version": "2.0",
                "members": [
                    {
                        "id": "peer-1",
                        "type": "ai_peer",
                        "name": "骨架伙伴",
                        "era": "当代",
                        "mbti": "INTJ",
                        "role": "been-there",
                        "one_line_concept": "初始设定",
                        "appearance_id": "xqg-intj-architect",
                        "source": "conceptual",
                        "persona_status": "skeleton",
                    }
                ],
            },
        )
        stale_session.add(room)
        await stale_session.flush()
        stale_session.add(
            RoomCharacter(
                id="peer-1",
                room_id=room.id,
                slot=0,
                role="been-there",
                source="conceptual",
                name="骨架伙伴",
                era="当代",
                mbti="INTJ",
                appearance_id="xqg-intj-architect",
                one_line_concept="初始设定",
                status="skeleton",
            )
        )
        await stale_session.commit()

        cached_character = await stale_session.get(RoomCharacter, "peer-1")
        assert cached_character is not None
        initial = await room_service.get_room_snapshot(room.id, user_id, stale_session)
        assert initial["stage_state"]["members"][0]["name"] == "骨架伙伴"

        async with async_session_factory() as background_session:
            await background_session.execute(
                update(RoomCharacter)
                .where(RoomCharacter.id == "peer-1")
                .values(
                    name="最终伙伴",
                    mbti="ENFP",
                    appearance_id="xqg-enfp-campaigner",
                    source="fallback",
                    status="ready",
                    one_line_concept="最终设定",
                )
            )
            await background_session.commit()

        refreshed = await room_service.get_room_snapshot(room.id, user_id, stale_session)
        member = refreshed["stage_state"]["members"][0]
        assert member["name"] == "最终伙伴"
        assert member["mbti"] == "ENFP"
        assert member["appearance_id"] == "xqg-enfp-campaigner"
        assert member["source"] == "fallback"
        assert member["persona_status"] == "ready"
        assert member["one_line_concept"] == "最终设定"


async def test_stage_b_team_update_carries_the_committed_room_snapshot(
    client: AsyncClient, monkeypatch
) -> None:
    events: list[dict[str, object]] = []

    async def capture_event(_room_id: str, payload: dict[str, object]) -> bool:
        events.append(payload)
        return True

    monkeypatch.setattr(
        character_generation_service.connection_manager,
        "send_to_room",
        capture_event,
    )
    _, headers = await register_user(client, "成员推送用户")
    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "等待最终成员资料"},
    )
    assert started.status_code == 201, started.text

    for _ in range(100):
        team_event = next(
            (event for event in events if event.get("type") == "team_updated"),
            None,
        )
        if team_event is not None:
            break
        await asyncio.sleep(0.02)

    assert team_event is not None
    assert team_event["data"]["room_id"] == started.json()["room_id"]
    members = team_event["data"]["stage_state"]["members"]
    assert len(members) == 5
    assert all(
        member.get("persona_status") == "ready"
        for member in members
        if member["type"] == "ai_peer"
    )
    assert all("system_prompt_template" not in member for member in members)


async def test_stage_b_fallback_publishes_the_calibrated_team_snapshot(
    client: AsyncClient, monkeypatch
) -> None:
    events: list[dict[str, object]] = []

    async def capture_event(_room_id: str, payload: dict[str, object]) -> bool:
        events.append(payload)
        return True

    async def fail_persona(*_args, **_kwargs):
        raise TimeoutError("persona timeout")

    monkeypatch.setattr(
        character_generation_service.connection_manager,
        "send_to_room",
        capture_event,
    )
    monkeypatch.setattr(
        Container.get().agent_runtime,
        "generate_conceptual_persona",
        fail_persona,
    )
    _, headers = await register_user(client, "成员降级推送用户")
    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "即使超时也要看到正确伙伴"},
    )
    assert started.status_code == 201, started.text

    for _ in range(100):
        team_event = next(
            (event for event in events if event.get("type") == "team_updated"),
            None,
        )
        if team_event is not None:
            break
        await asyncio.sleep(0.02)

    assert team_event is not None
    peers = [
        member
        for member in team_event["data"]["stage_state"]["members"]
        if member["type"] == "ai_peer"
    ]
    assert all(member.get("persona_status") == "ready" for member in peers)
    assert any(member.get("source") == "fallback" for member in peers)


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
    # 开场话术由后台任务异步产生,等到 AI_INTRO 消息落到 messages 再继续断言。
    room_id = started.json()["room_id"]
    for _ in range(100):
        if any(m["stage"] == "AI_INTRO" for m in snapshot.get("messages", [])):
            break
        await asyncio.sleep(0.05)
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
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


async def test_first_ai_turn_is_published_before_persona_preparation_finishes(
    client: AsyncClient, monkeypatch
) -> None:
    user_id, headers = await register_user(client, "首位 AI 状态用户")
    persona_waiting = asyncio.Event()
    release_personas = asyncio.Event()

    def put_human_last(_random, member_ids: list[str]) -> None:
        member_ids.sort(key=lambda member_id: member_id == user_id)

    async def wait_for_personas(_room_id: str) -> dict[str, dict[str, object]]:
        persona_waiting.set()
        await release_personas.wait()
        return {}

    monkeypatch.setattr(activity_service.SystemRandom, "shuffle", put_human_last)
    monkeypatch.setattr(
        character_generation_service, "ensure_personas_ready", wait_for_personas
    )

    started = await client.post(
        "/api/activities",
        headers=headers,
        json={"mood": "想听第一位 AI 团友慢慢分享"},
    )
    assert started.status_code == 201, started.text
    await asyncio.wait_for(persona_waiting.wait(), timeout=1)
    room_id = started.json()["room_id"]
    snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()

    try:
        assert snapshot["stage"] == "PAINTING_TURN"
        assert snapshot["stage_state"]["generation_status"] == "processing"
        assert snapshot["stage_state"]["current_speaker_id"] != user_id
        assert snapshot["stage_state"]["current_speaker_id"] == snapshot["stage_state"][
            "speaking_order"
        ][0]
    finally:
        release_personas.set()
        await _wait_for_painting_turn(client, room_id, headers)


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
    snapshot = await _wait_for_painting_turn(client, room_id, headers)
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
    assert submitted.json()["snapshot"]["stage_state"]["generation_status"] == "processing"
    completed = await _wait_for_stage(client, room_id, headers, "ARTWORK_REVEAL")
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
    snapshot = await _wait_for_painting_turn(client, room_id, headers)
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
    assert painted.status_code == 200, painted.text
    painted_snapshot = await _wait_for_stage(client, room_id, headers, "ARTWORK_REVEAL")

    revealed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "reveal-complete",
            "type": "complete_artwork_reveal",
            "payload": {"expected_revision": painted_snapshot["revision"]},
        },
    )
    assert revealed.status_code == 200, revealed.text
    current = await _wait_for_human_or_completed(client, room_id, headers, 0)

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
        current = await _wait_for_human_or_completed(client, room_id, headers, round_index + 1)

    assert current["stage"] == "COMPLETED"
    assert runtime.calls["reflection_question"] - before.get("reflection_question", 0) == 4
    assert runtime.calls["reflection_answer"] - before.get("reflection_answer", 0) == 16
    for _ in range(100):
        async with async_session_factory() as session:
            remaining_characters = await session.scalar(
                select(func.count())
                .select_from(RoomCharacter)
                .where(RoomCharacter.room_id == room_id)
            )
        if remaining_characters == 0:
            break
        await asyncio.sleep(0.02)
    assert remaining_characters == 0
    completed_snapshot = (
        await client.get(f"/api/rooms/{room_id}", headers=headers)
    ).json()
    expected_members = {member["id"]: member for member in state["members"]}
    for member in completed_snapshot["stage_state"]["members"]:
        expected = expected_members[member["id"]]
        assert member["name"] == expected["name"]
        assert member.get("mbti") == expected.get("mbti")
        assert member.get("appearance_id") == expected.get("appearance_id")
        if member["type"] == "ai_peer":
            assert member.get("persona_status") == "ready"
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
        snapshot = await _wait_for_painting_turn(client, room_id, headers)
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
        assert painted.status_code == 200, painted.text
        painted_snapshot = await _wait_for_stage(client, room_id, headers, "ARTWORK_REVEAL")

        revealed = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": "reveal-complete",
                "type": "complete_artwork_reveal",
                "payload": {"expected_revision": painted_snapshot["revision"]},
            },
        )
        assert revealed.status_code == 200, revealed.text
        current = await _wait_for_human_or_completed(client, room_id, headers, 0)

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
            current = await _wait_for_human_or_completed(client, room_id, headers, round_index + 1)

        assert current["stage"] == "COMPLETED"
    finally:
        container.agent_runtime = original_runtime
