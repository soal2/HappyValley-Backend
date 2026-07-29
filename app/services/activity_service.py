"""Backend-owned painting relay orchestration with remote generation provenance."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from random import SystemRandom
from typing import Any

import structlog
import yaml
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import (
    AgentRuntime,
    ImageGenerator,
    ReflectionAnswerResult,
    SummaryResult,
    TextGenerationResult,
)
from app.config import get_settings
from app.domain.room_lock import room_lock_manager
from app.media.storage import MediaStorage
from app.models.postcard import GeneratedImage, Postcard
from app.models.room import ActivityRoom, RoomMessage
from app.models.user import User
from app.services import character_generation_service, matching_service, queue_service, room_service
from app.services.cleanup_service import CleanupService
from app.services.crisis_service import CrisisService
from app.services.speech_service import estimated_duration_ms, prepare_speech_meta, split_speech
from app.services.text_sanitizer import sanitize_user_text

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
logger = structlog.get_logger()

REFLECTION_INTENTS = [
    "看到共同画作时此刻最直接的感受",
    "画下自己的那一笔时心里的想法或这样画的原因",
    "此刻想对其他团体成员说的话",
    "听完所有成员反馈之后现在的心情",
]


def _activity_config() -> dict[str, Any]:
    path = Path(get_settings().ACTIVITY_CONFIG_PATH)
    with path.open(encoding="utf-8") as config_file:
        return dict(yaml.safe_load(config_file))


def _human_member(user: User) -> dict[str, Any]:
    if user.profile is None:
        raise HTTPException(status_code=403, detail="请先完善个人资料")
    return {
        "id": user.id,
        "type": "human",
        "name": user.username,
        "role": "first-contributor",
        "appearance_id": user.profile.appearance_id,
        "mbti": user.profile.mbti,
        "voice_id": "you",
    }


def _generation(result: TextGenerationResult | ReflectionAnswerResult) -> dict[str, str]:
    return result.generation.model_dump()


def _payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(status_code=422, detail="text 必须是 1 到 4000 字符的文本")
    # 静默清除脏话/侮辱词：绘画贡献、回顾发言等所有用户文本的唯一入口，
    # 在入库与喂给文本模型/生图之前净化，避免上游内容审核拒绝（如 451）。
    return sanitize_user_text(text.strip())


def _merge_display_fields(
    member: dict[str, Any], persona: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge latest display fields from a persona snapshot into a member dict."""
    if persona is None:
        return member
    return {
        **member,
        "name": persona["name"],
        "era": persona["era"],
        "mbti": persona["mbti"],
        "role": persona["role"],
        "source": persona["source"],
        "appearance_id": persona["appearance_id"],
        "persona_status": persona["status"],
    }


def _remember_command(state: dict[str, Any], command_id: str) -> None:
    processed = list(state.get("processed_command_ids", []))
    processed.append(command_id)
    state["processed_command_ids"] = processed[-100:]


async def _emit_snapshot(
    room: ActivityRoom,
    db: AsyncSession,
    emit: EventEmitter | None,
    event_type: str = "snapshot",
    **extra: object,
) -> None:
    if emit is None:
        return
    snapshot = await room_service.get_room_snapshot(room.id, room.user_id, db)
    try:
        await emit({"type": event_type, "data": snapshot, "revision": room.revision, **extra})
    except Exception as error:
        logger.warning(
            "activity.event_delivery_failed",
            room_id=room.id,
            revision=room.revision,
            event_type=event_type,
            error_type=type(error).__name__,
        )


async def _commit_state(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    emit: EventEmitter | None,
    event_type: str = "snapshot",
    **extra: object,
) -> None:
    room.stage_state = dict(state)
    room.revision += 1
    await db.flush()
    await db.commit()
    await _emit_snapshot(room, db, emit, event_type, **extra)


async def _remote_text(
    operation: Callable[[], Awaitable[TextGenerationResult]],
) -> TextGenerationResult:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return await operation()
        except Exception as error:
            last_error = error
    raise RuntimeError(str(last_error or "远端 LLM 生成失败"))


async def _remote_summary(
    operation: Callable[[], Awaitable[SummaryResult]],
) -> SummaryResult:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return await operation()
        except Exception as error:
            last_error = error
    raise RuntimeError(str(last_error or "远端 LLM 总结失败"))


def _speech_fits(meta: dict[str, object]) -> bool:
    total = meta.get("total_duration_ms")
    limit = meta.get("limit_ms")
    return isinstance(total, int) and isinstance(limit, int) and total <= limit


async def _remote_spoken_text(
    operation: Callable[[], Awaitable[TextGenerationResult]],
    voice_id: str,
    limit_ms: int,
    validator: Callable[[str], bool] | None = None,
) -> tuple[TextGenerationResult, dict[str, object]]:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            result = await operation()
            if validator is not None and not validator(result.text):
                raise ValueError("AI 发言不符合当前轮次格式")
            speech = await prepare_speech_meta(result.text, voice_id, limit_ms)
            if not _speech_fits(speech):
                raise ValueError("AI 发言超过语音时长限制")
            return result, speech
        except Exception as error:
            last_error = error
    raise RuntimeError(str(last_error or "远端 AI 发言生成失败"))


async def _generate_opening(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None = None,
) -> bool:
    config = _activity_config()
    room.current_stage = "AI_INTRO"
    state.update(
        current_speaker_id="facilitator",
        generation_status="processing",
        generation_error=None,
        can_skip=False,
    )
    await _commit_state(room, state, db, emit, "stage_changed")
    try:
        first_speaker = _member_for(state, str(state["speaking_order"][0]))
        result, speech = await _remote_spoken_text(
            lambda: agent_runtime.generate_opening(
                state["members"],
                list(config["opening"]["rules"]),
                first_speaker,
                max_duration_seconds=20,
            ),
            "facilitator",
            20_000,
        )
    except Exception as error:
        state["generation_status"] = "failed"
        state["generation_error"] = str(error)
        await _commit_state(
            room, state, db, emit, "error", error="generation_failed", recoverable=True
        )
        return False
    message = RoomMessage(
        room_id=room.id,
        sender_type="ai",
        sender_id="facilitator",
        text=result.text,
        stage="AI_INTRO",
        generation_meta=_generation(result),
        speech_meta=speech,
    )
    db.add(message)
    await db.flush()
    state.update(generation_status="ready", generation_error=None, can_skip=True)
    await _commit_state(room, state, db, emit, "message_created", message_id=message.id)
    return True


async def create_activity_room(
    user: User, mood: str, db: AsyncSession, agent_runtime: AgentRuntime
) -> ActivityRoom:
    config = _activity_config()
    crisis = await CrisisService.check_message(mood, "pending", user.id, db, persist=False)
    match_mood = "" if crisis else mood
    dynamic = get_settings().CHARACTER_GENERATION_MODE == "dynamic"
    state: dict[str, Any] = {
        "activity_type": config["activity_type"],
        "activity_version": config["version"],
        "template_version": config["postcard"]["template_version"],
        "mood": None if crisis else mood,
        "members": [],
        "speaking_order": [],
        "painting_turn_index": 0,
        "reflection_round_index": 0,
        "reflection_turn_index": 0,
        "reflection_rounds": [],
        "artwork_reveal_status": "pending",
        "facilitator": dict(config["facilitator"], id="facilitator", type="ai_facilitator"),
        "contributions": [],
        "processed_command_ids": [],
        "image_status": "not_started",
        "generation_status": "idle",
        "generation_error": None,
        "current_speaker_id": "facilitator",
        "can_skip": False,
    }
    if crisis:
        state["crisis"] = crisis

    if dynamic:
        # Create room first — room_characters FK requires the room to exist.
        room = await room_service.create_room(user.id, db, stage_state=state)
        # Stage A: select 2 real + 2 conceptual skeletons, persist to room_characters.
        member_slots = await character_generation_service.select_and_persist_skeletons(
            room, user, match_mood, db, agent_runtime
        )
        state["members"] = [_human_member(user)] + member_slots
        room.stage_state = state
        await db.flush()
    else:
        peers = await matching_service.match_team_for_user(user.id, match_mood, db, agent_runtime)
        voice_ids = ["zhu", "xiaoli", "zhang", "li"]
        state["members"] = [_human_member(user)] + [
            dict(peer, type="ai_peer", voice_id=voice_ids[index])
            for index, peer in enumerate(peers)
        ]
        room = await room_service.create_room(user.id, db, stage_state=state)

    # Build speaking order (random shuffle of all member IDs) for v2 flow.
    speaking_order = [str(member["id"]) for member in state["members"]]
    SystemRandom().shuffle(speaking_order)
    state["speaking_order"] = speaking_order
    room.stage_state = state
    await db.flush()

    if crisis:
        db.add(
            RoomMessage(
                room_id=room.id,
                sender_type="system",
                sender_id="crisis_detector",
                text="[crisis_triggered]",
                stage="CRISIS",
                is_crisis=True,
            )
        )
        await db.flush()
        await db.commit()
    else:
        opening_ready = await _generate_opening(room, state, db, agent_runtime)
        if dynamic:
            # Launch Stage B background persona generation (idempotent).
            await character_generation_service.start_for_room(room.id)
        if opening_ready:
            await _run_v2_painting_until_human(room, state, db, agent_runtime, None)
    return room


async def terminate_legacy_room_for_upgrade(
    room: ActivityRoom,
    db: AsyncSession,
    media_storage: MediaStorage,
) -> None:
    """Safely end an active V1 room that cannot acquire deterministic V2 cursors."""
    state = dict(room.stage_state or {})
    if room.status != "active" or str(state.get("activity_version")) == "2.0":
        return
    state["termination_reason"] = "flow_upgraded"
    room.stage_state = state
    room.current_stage = "TERMINATED"
    await CleanupService.cleanup_terminated_room(room.id, db, media_storage)
    terminated_state = dict(room.stage_state or {})
    terminated_state["termination_reason"] = "flow_upgraded"
    room.stage_state = terminated_state
    room.revision += 1
    await db.commit()


def _member_for(state: dict[str, Any], member_id: str) -> dict[str, Any]:
    for member in state.get("members", []):
        if str(member.get("id")) == member_id:
            return dict(member)
    raise RuntimeError("活动发言顺序包含未知成员")


def _valid_peer_painting(text: str) -> bool:
    return (
        len(text) <= 220
        and text.startswith(("我希望这幅画上有", "我想在这幅画上加上"))
        and "？" not in text
        and "?" not in text
    )


def _valid_reflection_question(index: int) -> Callable[[str], bool]:
    required_groups = [
        (("感受", "感觉", "心情"),),
        (("想", "念头", "原因", "为什么", "心里", "画下", "那一笔"),),
        (("其他成员", "其他人", "大家", "团友", "伙伴"), ("说", "想对", "告诉")),
        (("现在", "此刻"), ("心情", "感受")),
    ]

    def validate(text: str) -> bool:
        return (
            len(text) <= 100
            and any(mark in text for mark in ("？", "?"))
            and all(any(word in text for word in group) for group in required_groups[index])
        )

    return validate


async def _run_v2_painting_until_human(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> bool:
    order = [str(member_id) for member_id in state["speaking_order"]]
    contributions = list(state.get("contributions", []))
    index = int(state.get("painting_turn_index", len(contributions)))

    # Stage C: wait for background persona generation, apply fallback, enrich peers.
    persona_map: dict[str, dict[str, Any]] | None = None
    if get_settings().CHARACTER_GENERATION_MODE == "dynamic":
        persona_map = await character_generation_service.ensure_personas_ready(room.id)

    while index < len(order):
        member = _member_for(state, order[index])
        if persona_map is not None:
            persona = persona_map.get(str(member["id"]))
            if persona is not None:
                member = character_generation_service.enrich_peer_with_persona(member, persona)
        room.current_stage = "PAINTING_TURN"
        state.update(
            painting_turn_index=index,
            current_speaker_id=str(member["id"]),
            generation_status="ready" if member["type"] == "human" else "processing",
            generation_error=None,
            can_skip=False,
        )
        await _commit_state(room, state, db, emit, "stage_changed")
        if member["type"] == "human":
            return False
        previous = [str(item["text"]) for item in contributions]
        try:
            result, speech = await _remote_spoken_text(
                partial(
                    agent_runtime.generate_peer_turn,
                    member,
                    previous,
                    max_duration_seconds=10,
                ),
                str(member.get("voice_id") or "facilitator"),
                10_000,
                _valid_peer_painting,
            )
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return False
        message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id=str(member["id"]),
            text=result.text,
            stage="PAINTING_TURN",
            generation_meta=_generation(result),
            speech_meta=speech,
        )
        db.add(message)
        await db.flush()
        contributions.append(
            {
                "contribution_id": message.id,
                "round": index + 1,
                "member_id": str(member["id"]),
                "member_type": "ai_peer",
                "text": result.text,
                "created_at": datetime.now(UTC).isoformat(),
                "generation": _generation(result),
            }
        )
        index += 1
        state.update(
            contributions=contributions,
            painting_turn_index=index,
            generation_status="ready",
            generation_error=None,
        )
        await _commit_state(room, state, db, emit, "message_created", message_id=message.id)
    return True


async def _generate_v2_image(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
    emit: EventEmitter | None,
) -> None:
    contributions = list(state.get("contributions", []))
    final_prompt = _build_weighted_prompt(contributions)
    state.update(
        final_prompt=final_prompt,
        current_speaker_id=None,
        image_status="generating",
        generation_status="processing",
        generation_error=None,
    )
    image = GeneratedImage(
        room_id=room.id,
        prompt_history={"contributions": contributions},
        status="generating",
    )
    db.add(image)
    await db.flush()
    await _commit_state(room, state, db, emit, "stage_changed")
    result = await image_generator.generate(final_prompt)
    if result.status != "completed" or not result.data:
        image.status = "failed"
        image.generation_meta = result.generation.model_dump() if result.generation else None
        state.update(
            image_status="failed",
            generation_status="failed",
            generation_error=result.error or "真实图片模型生成失败",
        )
        await _commit_state(
            room, state, db, emit, "error", error="image_generation_failed", recoverable=True
        )
        return
    path = await media_storage.save(result.data, room.user_id, room.id, "generated")
    image.file_path = path
    image.status = "completed"
    image.generation_meta = result.generation.model_dump() if result.generation else None
    room.current_stage = "ARTWORK_REVEAL"
    state.update(
        artwork_path=path,
        image_status="completed",
        artwork_reveal_status="pending",
        generation_status="ready",
        generation_error=None,
        current_speaker_id=None,
    )
    await _commit_state(room, state, db, emit, "artwork_updated")


async def _room_transcript(room_id: str, db: AsyncSession) -> list[dict[str, object]]:
    result = await db.execute(
        select(RoomMessage)
        .where(RoomMessage.room_id == room_id, RoomMessage.is_crisis.is_(False))
        .order_by(RoomMessage.created_at, RoomMessage.id)
    )
    return [
        {
            "id": message.id,
            "sender_type": message.sender_type,
            "sender_id": message.sender_id,
            "text": message.text,
            "stage": message.stage,
        }
        for message in result.scalars()
    ]


async def _remote_reflection_answer(
    operation: Callable[[], Awaitable[Any]],
) -> Any:
    last_error: Exception | None = None
    for _ in range(2):
        try:
            return await operation()
        except Exception as error:
            last_error = error
    raise RuntimeError(str(last_error or "远端 LLM 回顾回答生成失败"))


def _reflection_round(state: dict[str, Any], index: int) -> dict[str, Any]:
    rounds = list(state.get("reflection_rounds", []))
    while len(rounds) <= index:
        rounds.append(
            {
                "index": len(rounds),
                "intent": REFLECTION_INTENTS[len(rounds)],
                "answers": [],
            }
        )
    state["reflection_rounds"] = rounds
    return dict(rounds[index])


def _store_reflection_round(state: dict[str, Any], index: int, round_state: dict[str, Any]) -> None:
    rounds = list(state.get("reflection_rounds", []))
    rounds[index] = round_state
    state["reflection_rounds"] = rounds


async def _complete_v2_activity(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> None:
    room.current_stage = "AI_SUMMARY"
    state.update(
        current_speaker_id="facilitator",
        generation_status="processing",
        generation_error=None,
    )
    await _commit_state(room, state, db, emit, "stage_changed")
    transcript = await _room_transcript(room.id, db)
    if not state.get("summary_complete"):
        try:
            summary = None
            summary_speech: dict[str, object] | None = None
            candidate = None
            candidate_speech: dict[str, object] | None = None
            for _ in range(2):
                candidate = await agent_runtime.generate_summary(
                    str(state.get("final_prompt", "")),
                    transcript,
                    max_duration_seconds=20,
                )
                candidate_speech = await prepare_speech_meta(
                    candidate.summary_text, "facilitator", 20_000
                )
                if _speech_fits(candidate_speech):
                    summary = candidate
                    summary_speech = candidate_speech
                    break
            if summary is None:
                kept_chunks: list[str] = []
                running_ms = 0
                for chunk in split_speech(candidate.summary_text if candidate else ""):
                    chunk_ms = estimated_duration_ms(chunk)
                    if running_ms + chunk_ms > 20_000:
                        break
                    kept_chunks.append(chunk)
                    running_ms += chunk_ms
                truncated = "".join(kept_chunks) or (
                    candidate.summary_text[:28] if candidate else ""
                )
                summary = SummaryResult(
                    summary_text=truncated,
                    encouragement_text=candidate.encouragement_text if candidate else "",
                    generation=candidate.generation if candidate else None,
                )
                summary_speech = await prepare_speech_meta(truncated, "facilitator", 20_000)
                logger.warning(
                    "activity.summary_truncated",
                    original_chars=len(candidate.summary_text) if candidate else 0,
                    truncated_chars=len(truncated),
                    duration_ms=summary_speech.get("total_duration_ms"),
                )
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        summary_message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id="facilitator",
            text=summary.summary_text,
            stage="AI_SUMMARY",
            generation_meta=summary.generation.model_dump() if summary.generation else None,
            speech_meta=summary_speech,
        )
        db.add(summary_message)
        await db.flush()
        state.update(
            summary_text=summary.summary_text,
            summary_complete=True,
            generation_status="ready",
        )
        await _commit_state(room, state, db, emit, "message_created", message_id=summary_message.id)

    room.current_stage = "POSTCARD_GENERATION"
    state.update(generation_status="processing", generation_error=None)
    await _commit_state(room, state, db, emit, "stage_changed")
    human_texts = [
        str(message["text"])
        for message in transcript
        if message["sender_type"] == "human" and message["stage"] == "REFLECTION_TURN"
    ]
    try:
        postcard_text = await _remote_text(
            lambda: agent_runtime.generate_postcard_text(
                str(state.get("summary_text", "")),
                "\n".join(human_texts) if human_texts else "用户本轮选择暂不分享",
            )
        )
    except Exception as error:
        state.update(generation_status="failed", generation_error=str(error))
        await _commit_state(
            room, state, db, emit, "error", error="generation_failed", recoverable=True
        )
        return
    members_by_id = {str(member["id"]): dict(member) for member in state.get("members", [])}
    ordered_members = [
        members_by_id[member_id]
        for member_id in state.get("speaking_order", [])
        if member_id in members_by_id
    ]
    postcard = Postcard(
        user_id=room.user_id,
        room_id=room.id,
        image_path=state.get("artwork_path"),
        artwork_text=str(state.get("final_prompt", "")),
        summary_text=str(state.get("summary_text", "")),
        encouragement_text=postcard_text.text,
        generation_meta=_generation(postcard_text),
        member_data=ordered_members,
        template_version=str(state["template_version"]),
    )
    db.add(postcard)
    await db.flush()
    state.update(postcard_id=postcard.id, generation_status="ready")
    room.stage_state = dict(state)
    room.current_stage = "COMPLETED"
    room.status = "completed"
    room.ended_at = datetime.now(UTC)
    room.revision += 1
    await db.flush()
    await db.commit()
    await _emit_snapshot(room, db, emit, "postcard_ready", postcard_id=postcard.id)
    await CleanupService.cleanup_after_postcard(room.id, db)
    await db.commit()


async def _run_v2_reflection_until_human(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> None:
    round_index = int(state.get("reflection_round_index", 0))
    round_state = _reflection_round(state, round_index)
    question_id = round_state.get("question_message_id")
    question_message = (
        await db.scalar(select(RoomMessage).where(RoomMessage.id == question_id))
        if question_id
        else None
    )
    if question_message is None:
        raise RuntimeError("当前回顾轮次缺少小晴问题")
    order = [str(member_id) for member_id in state["speaking_order"]]
    turn_index = int(state.get("reflection_turn_index", 0))
    while turn_index < len(order):
        member = _member_for(state, order[turn_index])
        room.current_stage = "REFLECTION_TURN"
        state.update(
            reflection_turn_index=turn_index,
            current_speaker_id=str(member["id"]),
            generation_status="ready" if member["type"] == "human" else "processing",
            generation_error=None,
        )
        await _commit_state(room, state, db, emit, "stage_changed")
        if member["type"] == "human":
            return
        transcript = await _room_transcript(room.id, db)
        available_ids = {str(item["id"]) for item in transcript}
        try:
            answer = None
            answer_speech: dict[str, object] | None = None
            for _ in range(2):
                candidate = await agent_runtime.generate_reflection_answer(
                    member,
                    question_message.text,
                    str(state.get("final_prompt", "")),
                    transcript,
                    max_duration_seconds=10,
                )
                valid_references = [
                    ref for ref in candidate.referenced_message_ids
                    if ref in available_ids
                ]
                if valid_references != candidate.referenced_message_ids:
                    logger.warning(
                        "activity.reflection_answer.filtered_invalid_references",
                        room_id=room.id,
                        member_id=str(member["id"]),
                        member_name=member.get("name"),
                        invalid_refs=[
                            ref
                            for ref in candidate.referenced_message_ids
                            if ref not in available_ids
                        ],
                    )
                    candidate = ReflectionAnswerResult(
                        text=candidate.text,
                        positive_feedback_type=candidate.positive_feedback_type,
                        referenced_message_ids=valid_references,
                        generation=candidate.generation,
                    )
                candidate_speech = await prepare_speech_meta(
                    candidate.text,
                    str(member.get("voice_id") or "facilitator"),
                    10_000,
                )
                if _speech_fits(candidate_speech):
                    answer = candidate
                    answer_speech = candidate_speech
                    break
            if answer is None or answer_speech is None:
                raise ValueError("AI 团友回顾回答超过语音时长限制")
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id=str(member["id"]),
            text=answer.text,
            stage="REFLECTION_TURN",
            generation_meta=_generation(answer),
            speech_meta=answer_speech,
        )
        db.add(message)
        await db.flush()
        answers = list(round_state.get("answers", []))
        answers.append(
            {
                "member_id": str(member["id"]),
                "status": "answered",
                "message_id": message.id,
                "positive_feedback_type": answer.positive_feedback_type,
                "referenced_message_ids": answer.referenced_message_ids,
            }
        )
        round_state["answers"] = answers
        _store_reflection_round(state, round_index, round_state)
        turn_index += 1
        state.update(
            reflection_turn_index=turn_index,
            generation_status="ready",
            generation_error=None,
        )
        await _commit_state(room, state, db, emit, "message_created", message_id=message.id)

    next_round = round_index + 1
    state.update(reflection_round_index=next_round, reflection_turn_index=0)
    await _commit_state(room, state, db, emit)
    if next_round >= len(REFLECTION_INTENTS):
        await _complete_v2_activity(room, state, db, agent_runtime, emit)
    else:
        await _start_v2_reflection_round(room, state, db, agent_runtime, emit)


async def _start_v2_reflection_round(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> None:
    round_index = int(state.get("reflection_round_index", 0))
    if round_index >= len(REFLECTION_INTENTS):
        await _complete_v2_activity(room, state, db, agent_runtime, emit)
        return
    round_state = _reflection_round(state, round_index)
    if not round_state.get("question_message_id"):
        room.current_stage = "REFLECTION_QUESTION"
        state.update(
            current_speaker_id="facilitator",
            generation_status="processing",
            generation_error=None,
        )
        await _commit_state(room, state, db, emit, "stage_changed")
        transcript = await _room_transcript(room.id, db)
        try:
            question, question_speech = await _remote_spoken_text(
                lambda: agent_runtime.generate_reflection_question(
                    round_index,
                    REFLECTION_INTENTS[round_index],
                    str(state.get("final_prompt", "")),
                    transcript,
                    max_duration_seconds=10,
                ),
                "facilitator",
                10_000,
                _valid_reflection_question(round_index),
            )
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id="facilitator",
            text=question.text,
            stage="REFLECTION_QUESTION",
            generation_meta=_generation(question),
            speech_meta=question_speech,
        )
        db.add(message)
        await db.flush()
        round_state["question_message_id"] = message.id
        _store_reflection_round(state, round_index, round_state)
        state.update(generation_status="ready", reflection_turn_index=0)
        await _commit_state(room, state, db, emit, "message_created", message_id=message.id)
    await _run_v2_reflection_until_human(room, state, db, agent_runtime, emit)


async def _detect_and_pause(
    text: str,
    room: ActivityRoom,
    state: dict[str, Any],
    user_id: str,
    command_id: str,
    db: AsyncSession,
    emit: EventEmitter | None,
) -> bool:
    crisis = await CrisisService.check_message(text, room.id, user_id, db)
    if crisis is None:
        return False
    state["crisis"] = crisis
    state["crisis_resume_stage"] = room.current_stage
    state["generation_status"] = "paused"
    _remember_command(state, command_id)
    await _commit_state(room, state, db, emit, "error", error="crisis_triggered", recoverable=True)
    return True


async def _run_relay(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
    emit: EventEmitter | None,
) -> None:
    peers = [member for member in state["members"] if member["type"] == "ai_peer"]

    # Stage C: wait for background persona generation, apply fallback, enrich peers.
    persona_map: dict[str, dict[str, Any]] | None = None
    if get_settings().CHARACTER_GENERATION_MODE == "dynamic":
        persona_map = await character_generation_service.ensure_personas_ready(room.id)

    contributions = list(state.get("contributions", []))
    completed_ai = max(0, len(contributions) - 1)
    for index in range(completed_ai, 4):
        peer = peers[index]
        if persona_map is not None:
            persona = persona_map.get(str(peer["id"]))
            if persona is not None:
                peer = character_generation_service.enrich_peer_with_persona(peer, persona)
        stage = f"AI_TURN_{index + 1}"
        room.current_stage = stage
        state.update(
            current_speaker_id=str(peer["id"]),
            generation_status="processing",
            generation_error=None,
            can_skip=False,
        )
        await _commit_state(room, state, db, emit, "stage_changed")
        previous = [str(item["text"]) for item in contributions]
        try:
            result = await _remote_text(partial(agent_runtime.generate_peer_turn, peer, previous))
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id=str(peer["id"]),
            text=result.text,
            stage=stage,
            generation_meta=_generation(result),
        )
        db.add(message)
        await db.flush()
        contribution = {
            "contribution_id": message.id,
            "round": index + 2,
            "member_id": str(peer["id"]),
            "member_type": "ai_peer",
            "text": result.text,
            "created_at": datetime.now(UTC).isoformat(),
            "generation": _generation(result),
        }
        contributions.append(contribution)
        state.update(contributions=contributions, generation_status="ready", generation_error=None)
        await _commit_state(room, state, db, emit, "message_created", message_id=message.id)

    final_prompt = _build_weighted_prompt(contributions)
    room.current_stage = "IMAGE_GENERATION"
    state.update(
        final_prompt=final_prompt,
        current_speaker_id=None,
        image_status="generating",
        generation_status="processing",
        generation_error=None,
    )
    image = GeneratedImage(
        room_id=room.id, prompt_history={"contributions": contributions}, status="generating"
    )
    db.add(image)
    await db.flush()
    await _commit_state(room, state, db, emit, "stage_changed")
    image_result = await image_generator.generate(final_prompt)
    if image_result.status == "completed" and image_result.data:
        image.file_path = await media_storage.save(
            image_result.data, room.user_id, room.id, "generated"
        )
        image.status = "completed"
        image.generation_meta = (
            image_result.generation.model_dump() if image_result.generation else None
        )
        state.update(
            image_status="completed",
            artwork_path=image.file_path,
            generation_status="ready",
            generation_error=None,
        )
    else:
        image.status = "failed"
        image.generation_meta = (
            image_result.generation.model_dump() if image_result.generation else None
        )
        state.update(
            image_status="failed",
            generation_status="failed",
            generation_error=image_result.error or "真实图片模型生成失败",
        )
    room.current_stage = "HUMAN_SHARING"
    state.update(current_speaker_id=room.user_id, can_skip=False)
    await _commit_state(room, state, db, emit, "artwork_updated")


async def _run_completion(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> None:
    if not state.get("facilitator_response_complete"):
        room.current_stage = "AI_FOLLOW_UP"
        state.update(
            current_speaker_id="facilitator",
            generation_status="processing",
            generation_error=None,
        )
        await _commit_state(room, state, db, emit, "stage_changed")
        try:
            response = await _remote_text(
                lambda: agent_runtime.generate_facilitator_response(
                    str(state.get("final_prompt", "")), str(state["user_sharing_text"])
                )
            )
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        follow_message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id="facilitator",
            text=response.text,
            stage="AI_FOLLOW_UP",
            generation_meta=_generation(response),
        )
        db.add(follow_message)
        await db.flush()
        state["facilitator_response_complete"] = True
        await _commit_state(room, state, db, emit, "message_created", message_id=follow_message.id)

    room.current_stage = "AI_SUMMARY"
    state.update(generation_status="processing", generation_error=None)
    await _commit_state(room, state, db, emit, "stage_changed")
    summary_text = str(state.get("summary_text", ""))
    if not state.get("summary_complete"):
        transcript_result = await db.execute(
            select(RoomMessage)
            .where(RoomMessage.room_id == room.id)
            .order_by(RoomMessage.created_at)
        )
        transcript: list[dict[str, object]] = [
            {
                "sender_type": message.sender_type,
                "sender_id": message.sender_id,
                "text": message.text,
            }
            for message in transcript_result.scalars()
            if not message.is_crisis
        ]
        try:
            summary = await _remote_summary(
                lambda: agent_runtime.generate_summary(
                    str(state.get("final_prompt", "")), transcript
                )
            )
        except Exception as error:
            state.update(generation_status="failed", generation_error=str(error))
            await _commit_state(
                room, state, db, emit, "error", error="generation_failed", recoverable=True
            )
            return
        summary_text = summary.summary_text
        summary_message = RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id="facilitator",
            text=summary_text,
            stage="AI_SUMMARY",
            generation_meta=summary.generation.model_dump() if summary.generation else None,
        )
        db.add(summary_message)
        state.update(summary_text=summary_text, summary_complete=True)
        await db.flush()
        await _commit_state(room, state, db, emit, "message_created", message_id=summary_message.id)

    room.current_stage = "POSTCARD_GENERATION"
    state.update(generation_status="processing", generation_error=None)
    await _commit_state(room, state, db, emit, "stage_changed")
    try:
        postcard_text = await _remote_text(
            lambda: agent_runtime.generate_postcard_text(
                summary_text, str(state["user_sharing_text"])
            )
        )
    except Exception as error:
        state.update(generation_status="failed", generation_error=str(error))
        await _commit_state(
            room, state, db, emit, "error", error="generation_failed", recoverable=True
        )
        return
    # Dynamic mode: merge latest display fields (incl. fallback name changes) into
    # postcard member_data so the saved postcard shows the final teammate names.
    postcard_members = list(state["members"])
    if get_settings().CHARACTER_GENERATION_MODE == "dynamic":
        persona_map = await character_generation_service.ensure_personas_ready(room.id)
        postcard_members = [
            _merge_display_fields(member, persona_map.get(str(member.get("id"))))
            if member.get("type") == "ai_peer"
            else member
            for member in postcard_members
        ]
    postcard = Postcard(
        user_id=room.user_id,
        room_id=room.id,
        image_path=state.get("artwork_path"),
        artwork_text=str(state.get("final_prompt", "")),
        summary_text=summary_text,
        encouragement_text=postcard_text.text,
        generation_meta=_generation(postcard_text),
        member_data=postcard_members,
        template_version=str(state["template_version"]),
    )
    db.add(postcard)
    await db.flush()
    state["postcard_id"] = postcard.id
    state["generation_status"] = "ready"
    room.stage_state = dict(state)
    room.current_stage = "COMPLETED"
    room.status = "completed"
    room.ended_at = datetime.now(UTC)
    room.revision += 1
    await db.flush()
    await db.commit()
    await _emit_snapshot(room, db, emit, "postcard_ready", postcard_id=postcard.id)
    await CleanupService.cleanup_after_postcard(room.id, db)
    await db.commit()


async def _retry_image(
    room: ActivityRoom,
    state: dict[str, Any],
    db: AsyncSession,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
    emit: EventEmitter | None,
) -> None:
    if not state.get("final_prompt"):
        raise HTTPException(status_code=409, detail="尚无可重试的最终提示词")
    room.current_stage = "IMAGE_GENERATION"
    state.update(image_status="generating", generation_status="processing", generation_error=None)
    await _commit_state(room, state, db, emit, "stage_changed")
    result = await image_generator.generate(str(state["final_prompt"]))
    if result.status != "completed" or not result.data:
        state.update(
            image_status="failed", generation_status="failed", generation_error=result.error
        )
    else:
        path = await media_storage.save(result.data, room.user_id, room.id, "generated")
        db.add(
            GeneratedImage(
                room_id=room.id,
                prompt_history=None,
                status="completed",
                file_path=path,
                generation_meta=result.generation.model_dump() if result.generation else None,
            )
        )
        state.update(
            image_status="completed",
            artwork_path=path,
            generation_status="ready",
            generation_error=None,
        )
    if str(state.get("activity_version")) == "2.0" and state.get("image_status") == "completed":
        room.current_stage = "ARTWORK_REVEAL"
        state.update(
            current_speaker_id=None,
            artwork_reveal_status="pending",
        )
    elif str(state.get("activity_version")) == "2.0":
        room.current_stage = "IMAGE_GENERATION"
        state["current_speaker_id"] = None
    else:
        room.current_stage = "HUMAN_SHARING"
        state["current_speaker_id"] = room.user_id
    await _commit_state(room, state, db, emit, "artwork_updated")


def _build_weighted_prompt(contributions: list[dict[str, Any]]) -> str:
    """Join all contributions; emphasize human player's text by repeating at end."""
    all_texts = [str(item["text"]) for item in contributions]
    base = "；".join(all_texts)
    human_texts = [
        str(item["text"])
        for item in contributions
        if item.get("member_type") == "human"
    ]
    if human_texts:
        base += "。画面中必须重点体现：" + "，".join(human_texts)
    return base


async def _maybe_emotional_checkin(
    room: ActivityRoom,
    state: dict[str, Any],
    user_text: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    emit: EventEmitter | None,
) -> None:
    """Check user's text for negative emotion; insert facilitator response if needed."""
    contributions = state.get("contributions", [])
    context = "；".join(str(item["text"]) for item in contributions) if contributions else ""
    try:
        checkin = await agent_runtime.generate_emotional_checkin(user_text, context)
    except Exception as error:
        logger.warning("activity.emotional_checkin_failed", error=str(error))
        return
    if not checkin.needs_response or not checkin.text.strip():
        return
    message = RoomMessage(
        room_id=room.id,
        sender_type="ai",
        sender_id="facilitator",
        text=checkin.text,
        stage=room.current_stage,
        generation_meta=checkin.generation.model_dump(),
    )
    db.add(message)
    await db.flush()
    await _commit_state(room, state, db, emit, "message_created", message_id=message.id)


async def execute_command(
    room_id: str,
    user_id: str,
    command_id: str,
    command_type: str,
    payload: dict[str, Any],
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
    emit: EventEmitter | None = None,
) -> str:
    release_capacity = False
    async with room_lock_manager.acquire(room_id):
        room = await db.scalar(
            select(ActivityRoom).where(ActivityRoom.id == room_id).with_for_update()
        )
        if room is None:
            raise HTTPException(status_code=404, detail="房间不存在")
        if room.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问此房间")
        state = dict(room.stage_state or {})
        if command_id in state.get("processed_command_ids", []):
            if command_type == "submit_base" and (
                room.current_stage.startswith("AI_TURN_")
                or room.current_stage == "IMAGE_GENERATION"
            ):
                await _run_relay(
                    room, state, db, agent_runtime, image_generator, media_storage, emit
                )
            elif command_type == "submit_sharing" and room.current_stage in {
                "AI_FOLLOW_UP",
                "AI_SUMMARY",
                "POSTCARD_GENERATION",
            }:
                await _run_completion(room, state, db, agent_runtime, emit)
            return "already_processed"
        if room.status != "active" and command_type != "heartbeat":
            raise HTTPException(status_code=409, detail="活动已结束")
        if state.get("crisis") and command_type not in {"crisis_response", "heartbeat", "exit"}:
            raise HTTPException(status_code=409, detail="请先回应安全提示")

        reconnect_cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=get_settings().RECONNECT_SECONDS
        )
        if (
            room.status == "active"
            and room.disconnected_at is not None
            and room.disconnected_at.replace(tzinfo=None) < reconnect_cutoff
        ):
            state["termination_reason"] = "reconnect_timeout"
            room.stage_state = state
            room.current_stage = "TERMINATED"
            await CleanupService.cleanup_terminated_room(room.id, db, media_storage)
            room.revision += 1
            await db.commit()
            await _emit_snapshot(room, db, emit, "stage_changed")
            release_capacity = True
        elif command_type == "heartbeat":
            room.heartbeat_at = datetime.now(UTC)
            room.disconnected_at = None
            await db.commit()
        elif command_type == "exit":
            state["termination_reason"] = "user_exit"
            _remember_command(state, command_id)
            room.stage_state = state
            room.current_stage = "TERMINATED"
            await CleanupService.cleanup_terminated_room(room.id, db, media_storage)
            room.revision += 1
            await db.commit()
            await _emit_snapshot(room, db, emit, "stage_changed")
            release_capacity = True
        elif command_type == "crisis_response":
            can_continue = payload.get("can_continue")
            if not isinstance(can_continue, bool):
                raise HTTPException(status_code=422, detail="can_continue 必须是布尔值")
            if can_continue:
                state.pop("crisis", None)
                state.pop("crisis_waiting_since", None)
                room.disconnected_at = None
                resume_stage = str(state.pop("crisis_resume_stage", room.current_stage))
                _remember_command(state, command_id)
                if resume_stage == "AI_INTRO":
                    opening_ready = bool(state.get("can_skip")) or await _generate_opening(
                        room, state, db, agent_runtime, emit
                    )
                    if opening_ready and str(state.get("activity_version")) == "2.0":
                        await _run_v2_painting_until_human(room, state, db, agent_runtime, emit)
                else:
                    room.current_stage = resume_stage
                    state.update(generation_status="ready", generation_error=None)
                    await _commit_state(room, state, db, emit, "stage_changed")
            else:
                state["crisis_waiting_since"] = datetime.now(UTC).isoformat()
                room.disconnected_at = datetime.now(UTC)
                _remember_command(state, command_id)
                await _commit_state(room, state, db, emit)
        elif command_type == "skip_current_stage":
            if str(state.get("activity_version")) == "2.0":
                raise HTTPException(status_code=409, detail="V2 活动开场会自动进入随机绘画轮次")
            expected_stage = payload.get("expected_stage")
            expected_revision = payload.get("expected_revision")
            if expected_stage != room.current_stage or expected_revision != room.revision:
                raise HTTPException(status_code=409, detail="阶段或版本已变化，请使用最新快照")
            if room.current_stage != "AI_INTRO" or not state.get("can_skip"):
                raise HTTPException(status_code=409, detail="当前阶段不能跳过")
            room.current_stage = "HUMAN_BASE_PAINTING"
            state.update(current_speaker_id=room.user_id, can_skip=False, generation_status="ready")
            _remember_command(state, command_id)
            await _commit_state(room, state, db, emit, "stage_changed")
        elif command_type == "complete_artwork_reveal":
            if room.current_stage != "ARTWORK_REVEAL":
                raise HTTPException(status_code=409, detail="当前没有等待确认的共同画作")
            expected_revision = payload.get("expected_revision")
            if expected_revision != room.revision:
                raise HTTPException(status_code=409, detail="画作状态已变化，请使用最新快照")
            state["artwork_reveal_status"] = "acknowledged"
            _remember_command(state, command_id)
            await _commit_state(room, state, db, emit)
            await _start_v2_reflection_round(room, state, db, agent_runtime, emit)
            release_capacity = room.status == "completed"
        elif command_type == "submit_painting_turn":
            if room.current_stage != "PAINTING_TURN":
                raise HTTPException(status_code=409, detail="当前不是绘画发言阶段")
            turn_index = payload.get("turn_index")
            expected_index = int(state.get("painting_turn_index", -1))
            if turn_index != expected_index:
                raise HTTPException(status_code=409, detail="绘画轮次已变化，请使用最新快照")
            order = [str(member_id) for member_id in state.get("speaking_order", [])]
            if expected_index < 0 or expected_index >= len(order):
                raise HTTPException(status_code=409, detail="绘画轮次状态无效")
            member = _member_for(state, order[expected_index])
            if member["type"] != "human" or str(member["id"]) != user_id:
                raise HTTPException(status_code=409, detail="当前尚未轮到真人发言")
            text = _payload_text(payload)
            if not await _detect_and_pause(text, room, state, user_id, command_id, db, emit):
                message = RoomMessage(
                    room_id=room.id,
                    sender_type="human",
                    sender_id=user_id,
                    text=text,
                    stage="PAINTING_TURN",
                )
                db.add(message)
                await db.flush()
                contributions = list(state.get("contributions", []))
                contributions.append(
                    {
                        "contribution_id": message.id,
                        "round": expected_index + 1,
                        "member_id": user_id,
                        "member_type": "human",
                        "text": text,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                )
                state.update(
                    contributions=contributions,
                    painting_turn_index=expected_index + 1,
                )
                _remember_command(state, command_id)
                await _maybe_emotional_checkin(room, state, text, db, agent_runtime, emit)
                complete = await _run_v2_painting_until_human(room, state, db, agent_runtime, emit)
                if complete:
                    await _generate_v2_image(room, state, db, image_generator, media_storage, emit)
        elif command_type in {"submit_reflection", "skip_reflection"}:
            if room.current_stage != "REFLECTION_TURN":
                raise HTTPException(status_code=409, detail="当前不是团体回顾发言阶段")
            round_index = payload.get("round_index")
            expected_round = int(state.get("reflection_round_index", -1))
            turn_index = int(state.get("reflection_turn_index", -1))
            if round_index != expected_round:
                raise HTTPException(status_code=409, detail="回顾问题已变化，请使用最新快照")
            order = [str(member_id) for member_id in state.get("speaking_order", [])]
            if turn_index < 0 or turn_index >= len(order):
                raise HTTPException(status_code=409, detail="回顾轮次状态无效")
            member = _member_for(state, order[turn_index])
            if member["type"] != "human" or str(member["id"]) != user_id:
                raise HTTPException(status_code=409, detail="当前尚未轮到真人发言")
            text = ""
            if command_type == "submit_reflection":
                text = _payload_text(payload)
                if await _detect_and_pause(text, room, state, user_id, command_id, db, emit):
                    return "accepted"
            round_state = _reflection_round(state, expected_round)
            answers = list(round_state.get("answers", []))
            answer_record: dict[str, Any] = {
                "member_id": user_id,
                "status": "skipped" if command_type == "skip_reflection" else "answered",
            }
            if command_type == "submit_reflection":
                message = RoomMessage(
                    room_id=room.id,
                    sender_type="human",
                    sender_id=user_id,
                    text=text,
                    stage="REFLECTION_TURN",
                )
                db.add(message)
                await db.flush()
                answer_record["message_id"] = message.id
            answers.append(answer_record)
            round_state["answers"] = answers
            _store_reflection_round(state, expected_round, round_state)
            state["reflection_turn_index"] = turn_index + 1
            _remember_command(state, command_id)
            if command_type == "submit_reflection" and text:
                await _maybe_emotional_checkin(room, state, text, db, agent_runtime, emit)
            await _run_v2_reflection_until_human(room, state, db, agent_runtime, emit)
            release_capacity = room.status == "completed"
        elif command_type == "submit_base":
            if str(state.get("activity_version")) == "2.0":
                raise HTTPException(status_code=409, detail="V2 活动请使用 submit_painting_turn")
            if room.current_stage != "HUMAN_BASE_PAINTING":
                raise HTTPException(status_code=409, detail="当前不是基础画面提交阶段")
            text = _payload_text(payload)
            if not await _detect_and_pause(text, room, state, user_id, command_id, db, emit):
                message = RoomMessage(
                    room_id=room.id,
                    sender_type="human",
                    sender_id=room.user_id,
                    text=text,
                    stage="HUMAN_BASE_PAINTING",
                )
                db.add(message)
                await db.flush()
                state["contributions"] = [
                    {
                        "contribution_id": message.id,
                        "round": 1,
                        "member_id": room.user_id,
                        "member_type": "human",
                        "text": text,
                        "created_at": datetime.now(UTC).isoformat(),
                    }
                ]
                _remember_command(state, command_id)
                await _maybe_emotional_checkin(room, state, text, db, agent_runtime, emit)
                await _run_relay(
                    room, state, db, agent_runtime, image_generator, media_storage, emit
                )
        elif command_type == "submit_sharing":
            if room.current_stage != "HUMAN_SHARING":
                raise HTTPException(status_code=409, detail="当前不是作品分享阶段")
            text = _payload_text(payload)
            if not await _detect_and_pause(text, room, state, user_id, command_id, db, emit):
                db.add(
                    RoomMessage(
                        room_id=room.id,
                        sender_type="human",
                        sender_id=room.user_id,
                        text=text,
                        stage="HUMAN_SHARING",
                    )
                )
                state["user_sharing_text"] = text
                _remember_command(state, command_id)
                await _maybe_emotional_checkin(room, state, text, db, agent_runtime, emit)
                await _run_completion(room, state, db, agent_runtime, emit)
                release_capacity = room.status == "completed"
        elif command_type == "retry_current_stage":
            if state.get("generation_status") != "failed":
                raise HTTPException(status_code=409, detail="当前没有可重试的生成任务")
            _remember_command(state, command_id)
            if room.current_stage == "AI_INTRO":
                opening_ready = await _generate_opening(room, state, db, agent_runtime, emit)
                if opening_ready and str(state.get("activity_version")) == "2.0":
                    await _run_v2_painting_until_human(room, state, db, agent_runtime, emit)
            elif room.current_stage == "PAINTING_TURN":
                complete = await _run_v2_painting_until_human(room, state, db, agent_runtime, emit)
                if complete:
                    await _generate_v2_image(room, state, db, image_generator, media_storage, emit)
            elif room.current_stage == "REFLECTION_QUESTION":
                await _start_v2_reflection_round(room, state, db, agent_runtime, emit)
            elif room.current_stage == "REFLECTION_TURN":
                await _run_v2_reflection_until_human(room, state, db, agent_runtime, emit)
            elif (
                room.current_stage in {"AI_SUMMARY", "POSTCARD_GENERATION"}
                and str(state.get("activity_version")) == "2.0"
            ):
                await _complete_v2_activity(room, state, db, agent_runtime, emit)
            elif room.current_stage.startswith("AI_TURN_"):
                await _run_relay(
                    room, state, db, agent_runtime, image_generator, media_storage, emit
                )
            elif room.current_stage in {"AI_FOLLOW_UP", "AI_SUMMARY", "POSTCARD_GENERATION"}:
                await _run_completion(room, state, db, agent_runtime, emit)
            else:
                raise HTTPException(status_code=409, detail="当前阶段不支持重试")
        elif command_type == "retry_image":
            if state.get("image_status") != "failed":
                raise HTTPException(status_code=409, detail="当前没有失败的图片任务")
            _remember_command(state, command_id)
            await _retry_image(room, state, db, image_generator, media_storage, emit)
        elif command_type in {"answer_follow_up", "skip_follow_up"}:
            raise HTTPException(status_code=409, detail="当前活动版本不会向用户追加问题")
        else:
            raise HTTPException(status_code=422, detail="未知活动命令")

    if release_capacity:
        await queue_service.release_capacity(db)
    return "accepted"


async def resume_interrupted_activity(
    room_id: str,
    user_id: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
) -> bool:
    """Resume a committed in-progress stage after process or socket interruption."""
    completed = False
    resumed = False
    async with room_lock_manager.acquire(room_id):
        room = await db.scalar(
            select(ActivityRoom).where(ActivityRoom.id == room_id).with_for_update()
        )
        if room is None or room.user_id != user_id or room.status != "active":
            return False
        state = dict(room.stage_state or {})
        is_v2 = str(state.get("activity_version")) == "2.0"
        generation_processing = state.get("generation_status") == "processing"
        if room.current_stage == "AI_INTRO" and is_v2:
            resumed = True
            opening_ready = bool(state.get("can_skip"))
            if not opening_ready and generation_processing:
                opening_ready = await _generate_opening(room, state, db, agent_runtime, None)
            if opening_ready:
                await _run_v2_painting_until_human(room, state, db, agent_runtime, None)
        elif (
            room.current_stage == "ARTWORK_REVEAL"
            and is_v2
            and state.get("artwork_reveal_status") == "acknowledged"
        ):
            resumed = True
            await _start_v2_reflection_round(room, state, db, agent_runtime, None)
        elif room.current_stage == "PAINTING_TURN" and is_v2 and generation_processing:
            resumed = True
            complete = await _run_v2_painting_until_human(room, state, db, agent_runtime, None)
            if complete:
                await _generate_v2_image(room, state, db, image_generator, media_storage, None)
        elif room.current_stage == "REFLECTION_QUESTION" and is_v2 and generation_processing:
            resumed = True
            await _start_v2_reflection_round(room, state, db, agent_runtime, None)
        elif room.current_stage == "REFLECTION_TURN" and is_v2 and generation_processing:
            resumed = True
            await _run_v2_reflection_until_human(room, state, db, agent_runtime, None)
            completed = room.status == "completed"
        elif (
            room.current_stage in {"AI_SUMMARY", "POSTCARD_GENERATION"}
            and is_v2
            and generation_processing
        ):
            resumed = True
            await _complete_v2_activity(room, state, db, agent_runtime, None)
            completed = room.status == "completed"
        elif room.current_stage == "IMAGE_GENERATION" and is_v2 and generation_processing:
            resumed = True
            await _retry_image(room, state, db, image_generator, media_storage, None)
        elif not is_v2 and (
            room.current_stage.startswith("AI_TURN_") or room.current_stage == "IMAGE_GENERATION"
        ):
            resumed = True
            await _run_relay(room, state, db, agent_runtime, image_generator, media_storage, None)
        elif room.current_stage in {"AI_FOLLOW_UP", "AI_SUMMARY", "POSTCARD_GENERATION"}:
            if state.get("generation_status") == "processing":
                resumed = True
                await _run_completion(room, state, db, agent_runtime, None)
                completed = room.status == "completed"
    if completed:
        await queue_service.release_capacity(db)
    return resumed
