"""Backend-owned painting relay orchestration with remote generation provenance."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import structlog
import yaml
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentRuntime, ImageGenerator, SummaryResult, TextGenerationResult
from app.config import get_settings
from app.domain.room_lock import room_lock_manager
from app.media.storage import MediaStorage
from app.models.postcard import GeneratedImage, Postcard
from app.models.room import ActivityRoom, RoomMessage
from app.models.user import User
from app.services import matching_service, queue_service, room_service
from app.services.cleanup_service import CleanupService
from app.services.crisis_service import CrisisService

EventEmitter = Callable[[dict[str, Any]], Awaitable[None]]
logger = structlog.get_logger()


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


def _generation(result: TextGenerationResult) -> dict[str, str]:
    return result.generation.model_dump()


def _payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(status_code=422, detail="text 必须是 1 到 4000 字符的文本")
    return text.strip()


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
        result = await _remote_text(
            lambda: agent_runtime.generate_opening(
                state["members"], list(config["opening"]["rules"])
            )
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
    peers = await matching_service.match_team_for_user(user.id, match_mood, db, agent_runtime)
    voice_ids = ["zhu", "xiaoli", "zhang", "li"]
    members = [_human_member(user)] + [
        dict(peer, type="ai_peer", voice_id=voice_ids[index]) for index, peer in enumerate(peers)
    ]
    state: dict[str, Any] = {
        "activity_type": config["activity_type"],
        "activity_version": config["version"],
        "template_version": config["postcard"]["template_version"],
        "mood": None if crisis else mood,
        "members": members,
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
    room = await room_service.create_room(user.id, db, stage_state=state)
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
    else:
        await _generate_opening(room, state, db, agent_runtime)
    return room


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
    contributions = list(state.get("contributions", []))
    completed_ai = max(0, len(contributions) - 1)
    for index in range(completed_ai, 4):
        peer = peers[index]
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

    final_prompt = "；".join(str(item["text"]) for item in contributions)
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
    postcard = Postcard(
        user_id=room.user_id,
        room_id=room.id,
        image_path=state.get("artwork_path"),
        artwork_text=str(state.get("final_prompt", "")),
        summary_text=summary_text,
        encouragement_text=postcard_text.text,
        generation_meta=_generation(postcard_text),
        member_data=list(state["members"]),
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
    room.current_stage = "HUMAN_SHARING"
    state["current_speaker_id"] = room.user_id
    await _commit_state(room, state, db, emit, "artwork_updated")


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
                if resume_stage == "AI_INTRO" and not state.get("can_skip"):
                    await _generate_opening(room, state, db, agent_runtime, emit)
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
        elif command_type == "submit_base":
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
                await _run_completion(room, state, db, agent_runtime, emit)
                release_capacity = room.status == "completed"
        elif command_type == "retry_current_stage":
            if state.get("generation_status") != "failed":
                raise HTTPException(status_code=409, detail="当前没有可重试的生成任务")
            _remember_command(state, command_id)
            if room.current_stage == "AI_INTRO":
                await _generate_opening(room, state, db, agent_runtime, emit)
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
        if room.current_stage.startswith("AI_TURN_") or room.current_stage == "IMAGE_GENERATION":
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
