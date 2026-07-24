"""Backend-owned P0 painting activity creation and command orchestration."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentRuntime, ImageGenerator, SummaryResult
from app.config import get_settings
from app.domain.room_lock import room_lock_manager
from app.media.storage import MediaStorage
from app.models.postcard import GeneratedImage, Postcard
from app.models.room import ActivityRoom, RoomMessage
from app.models.user import User
from app.services import matching_service, queue_service, room_service
from app.services.cleanup_service import CleanupService
from app.services.crisis_service import CrisisService


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
    }


async def create_activity_room(
    user: User, mood: str, db: AsyncSession, agent_runtime: AgentRuntime
) -> ActivityRoom:
    config = _activity_config()
    crisis = await CrisisService.check_message(mood, "pending", user.id, db, persist=False)
    match_mood = "" if crisis else mood
    peers = await matching_service.match_team_for_user(user.id, match_mood, db, agent_runtime)
    members = [_human_member(user)] + [dict(peer, type="ai_peer") for peer in peers]
    state: dict[str, Any] = {
        "activity_type": config["activity_type"],
        "activity_version": config["version"],
        "template_version": config["postcard"]["template_version"],
        "group_photo_version": config["postcard"]["group_photo_version"],
        "mood": None if crisis else mood,
        "members": members,
        "processed_command_ids": [],
        "follow_up_count": 0,
        "image_status": "not_started",
    }
    if crisis:
        state["crisis"] = crisis
    room = await room_service.create_room(user.id, db, stage_state=state)
    db.add(
        RoomMessage(
            room_id=room.id,
            sender_type="system",
            sender_id="facilitator",
            text=config["opening"]["text"],
            stage="AI_INTRO",
        )
    )
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
    return room


def _payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise HTTPException(status_code=422, detail="text 必须是 1 到 4000 字符的文本")
    return text.strip()


def _remember_command(state: dict[str, Any], command_id: str) -> None:
    processed = list(state.get("processed_command_ids", []))
    processed.append(command_id)
    state["processed_command_ids"] = processed[-100:]


async def _detect_and_pause(
    text: str,
    room: ActivityRoom,
    state: dict[str, Any],
    user_id: str,
    command_id: str,
    db: AsyncSession,
) -> bool:
    crisis = await CrisisService.check_message(text, room.id, user_id, db)
    if crisis is None:
        return False
    state["crisis"] = crisis
    _remember_command(state, command_id)
    room.stage_state = state
    room.revision += 1
    await db.flush()
    return True


async def _run_relay(
    room: ActivityRoom,
    state: dict[str, Any],
    text: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
    image_generator: ImageGenerator,
    media_storage: MediaStorage,
) -> None:
    db.add(
        RoomMessage(
            room_id=room.id,
            sender_type="human",
            sender_id=room.user_id,
            text=text,
            stage="HUMAN_BASE_PAINTING",
        )
    )
    peers = [member for member in state["members"] if member["type"] == "ai_peer"]
    context = "只增加或修饰已有画面，不得删除、否定或覆盖前序成员的表达。"
    try:
        turns = await agent_runtime.generate_ai_turns(context, peers, [text])
        if len(turns) != 4 or any(not turn.strip() for turn in turns):
            raise ValueError("AI 接龙必须返回四条非空贡献")
    except Exception:
        turns = [f"{peer['name']}为画面增加了一束柔和的光" for peer in peers]
    contributions = [text]
    for index, (peer, turn) in enumerate(zip(peers, turns, strict=True), start=1):
        contributions.append(turn)
        db.add(
            RoomMessage(
                room_id=room.id,
                sender_type="ai",
                sender_id=str(peer["id"]),
                text=turn,
                stage=f"AI_TURN_{index}",
            )
        )
    final_prompt = "；".join(contributions)
    image = GeneratedImage(
        room_id=room.id,
        prompt_history={"contributions": contributions},
        status="generating",
    )
    db.add(image)
    await db.flush()
    result = await image_generator.generate(final_prompt)
    if result.status == "completed" and result.data:
        image.file_path = await media_storage.save(result.data, room.user_id, room.id, "generated")
        image.status = "completed"
        state["image_status"] = "completed"
        state["artwork_path"] = image.file_path
    else:
        image.status = "failed"
        state["image_status"] = "failed"
    state["base_text"] = text
    state["contributions"] = contributions
    state["final_prompt"] = final_prompt
    room.current_stage = "HUMAN_SHARING"


async def _run_sharing(
    room: ActivityRoom,
    state: dict[str, Any],
    text: str,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
) -> None:
    db.add(
        RoomMessage(
            room_id=room.id,
            sender_type="human",
            sender_id=room.user_id,
            text=text,
            stage="HUMAN_SHARING",
        )
    )
    follow_up = await agent_runtime.generate_follow_up(str(state.get("final_prompt", "")), text)
    db.add(
        RoomMessage(
            room_id=room.id,
            sender_type="ai",
            sender_id="facilitator",
            text=follow_up,
            stage="AI_FOLLOW_UP",
        )
    )
    state["user_sharing_text"] = text
    state["follow_up_text"] = follow_up
    state["follow_up_count"] = 1
    room.current_stage = "AI_FOLLOW_UP"


async def _complete_activity(
    room: ActivityRoom,
    state: dict[str, Any],
    answer: str | None,
    db: AsyncSession,
    agent_runtime: AgentRuntime,
) -> None:
    if answer:
        db.add(
            RoomMessage(
                room_id=room.id,
                sender_type="human",
                sender_id=room.user_id,
                text=answer,
                stage="AI_FOLLOW_UP",
            )
        )
    message_result = await db.execute(
        select(RoomMessage).where(RoomMessage.room_id == room.id).order_by(RoomMessage.created_at)
    )
    transcript: list[dict[str, object]] = [
        {"sender_type": message.sender_type, "sender_id": message.sender_id, "text": message.text}
        for message in message_result.scalars()
        if not message.is_crisis
    ]
    try:
        summary = await agent_runtime.generate_summary(
            str(state.get("final_prompt", "")), transcript
        )
    except Exception:
        summary = SummaryResult(
            summary_text="这幅共同作品记录了五位成员逐步叠加的表达。",
            encouragement_text="谢谢你认真完成这次活动，愿你带着这份温暖继续前行。",
        )
    try:
        encouragement = await agent_runtime.generate_postcard_text(
            summary.summary_text, str(state.get("user_sharing_text", ""))
        )
    except Exception:
        encouragement = summary.encouragement_text
    postcard = Postcard(
        user_id=room.user_id,
        room_id=room.id,
        image_path=state.get("artwork_path"),
        artwork_text=str(state.get("final_prompt", "")),
        summary_text=summary.summary_text,
        encouragement_text=encouragement,
        member_data=list(state["members"]),
        template_version=str(state["template_version"]),
    )
    db.add(postcard)
    await db.flush()
    state["postcard_id"] = postcard.id
    room.stage_state = state
    room.current_stage = "COMPLETED"
    room.status = "completed"
    room.ended_at = datetime.now(UTC)
    await db.flush()
    await CleanupService.cleanup_after_postcard(room.id, db)


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
) -> str:
    """Serialize commands per room; only this function may advance stages."""
    release_capacity = False
    timed_out = False
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
            return "already_processed"
        if room.status == "active" and room.disconnected_at:
            disconnected_at = room.disconnected_at
            if disconnected_at.tzinfo is None:
                disconnected_at = disconnected_at.replace(tzinfo=UTC)
            elapsed = (datetime.now(UTC) - disconnected_at).total_seconds()
            if elapsed >= get_settings().RECONNECT_SECONDS:
                state["termination_reason"] = "reconnect_timeout"
                room.stage_state = state
                room.current_stage = "TERMINATED"
                room.status = "terminated"
                room.ended_at = datetime.now(UTC)
                _remember_command(state, command_id)
                await db.flush()
                await CleanupService.cleanup_terminated_room(room.id, db, media_storage)
                room.revision += 1
                await db.flush()
                timed_out = True
                release_capacity = True
        if timed_out:
            pass
        if not timed_out and room.status != "active" and command_type != "heartbeat":
            raise HTTPException(status_code=409, detail="活动已结束")

        crisis = state.get("crisis")
        if crisis and command_type not in {"crisis_response", "heartbeat", "exit"}:
            raise HTTPException(status_code=409, detail="请先回应安全提示")

        if timed_out:
            pass
        elif command_type == "heartbeat":
            room.heartbeat_at = datetime.now(UTC)
            room.disconnected_at = None
        elif command_type == "exit":
            state["termination_reason"] = "user_exit"
            room.stage_state = state
            room.current_stage = "TERMINATED"
            room.status = "terminated"
            room.ended_at = datetime.now(UTC)
            _remember_command(state, command_id)
            await db.flush()
            await CleanupService.cleanup_terminated_room(room.id, db, media_storage)
            release_capacity = True
        elif command_type == "crisis_response":
            can_continue = payload.get("can_continue")
            if not isinstance(can_continue, bool):
                raise HTTPException(status_code=422, detail="can_continue 必须是布尔值")
            if can_continue:
                state.pop("crisis", None)
                state.pop("crisis_waiting_since", None)
                room.disconnected_at = None
            else:
                state["crisis_waiting_since"] = datetime.now(UTC).isoformat()
                room.disconnected_at = datetime.now(UTC)
            _remember_command(state, command_id)
            room.stage_state = state
        elif command_type == "submit_base":
            if room.current_stage != "HUMAN_BASE_PAINTING":
                raise HTTPException(status_code=409, detail="当前不是基础画面提交阶段")
            text = _payload_text(payload)
            if not await _detect_and_pause(text, room, state, user_id, command_id, db):
                await _run_relay(
                    room, state, text, db, agent_runtime, image_generator, media_storage
                )
                _remember_command(state, command_id)
                room.stage_state = state
        elif command_type == "submit_sharing":
            if room.current_stage != "HUMAN_SHARING":
                raise HTTPException(status_code=409, detail="当前不是作品分享阶段")
            text = _payload_text(payload)
            if not await _detect_and_pause(text, room, state, user_id, command_id, db):
                await _run_sharing(room, state, text, db, agent_runtime)
                _remember_command(state, command_id)
                room.stage_state = state
        elif command_type in {"answer_follow_up", "skip_follow_up"}:
            if room.current_stage != "AI_FOLLOW_UP":
                raise HTTPException(status_code=409, detail="当前没有可回答的追问")
            answer = _payload_text(payload) if command_type == "answer_follow_up" else None
            if answer and await _detect_and_pause(answer, room, state, user_id, command_id, db):
                pass
            else:
                _remember_command(state, command_id)
                await _complete_activity(room, state, answer, db, agent_runtime)
                release_capacity = True
        if not timed_out:
            room.revision += 1
            await db.flush()
        await db.commit()
    if release_capacity:
        await queue_service.release_capacity(db)
    return "accepted"
