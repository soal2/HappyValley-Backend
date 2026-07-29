"""Room service: CRUD and state management for activity rooms."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.activity_states import ActivityStage, RoomStatus
from app.domain.room_lock import room_lock_manager
from app.domain.state_machine import advance_stage
from app.models.room import ActivityRoom, RoomMessage
from app.models.room_character import RoomCharacter


async def create_room(
    user_id: str, db: AsyncSession, *, stage_state: dict[str, Any] | None = None
) -> ActivityRoom:
    """Create a new activity room in AI introduction stage."""
    room = ActivityRoom(
        user_id=user_id,
        status=RoomStatus.ACTIVE.value,
        current_stage=ActivityStage.AI_INTRO.value,
        stage_state=stage_state or {},
        started_at=datetime.now(UTC),
        heartbeat_at=datetime.now(UTC),
    )
    db.add(room)
    await db.flush()
    return room


async def get_room(room_id: str, user_id: str, db: AsyncSession) -> ActivityRoom:
    """Fetch room, verify ownership."""
    result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=404, detail="房间不存在")
    if room.user_id != user_id:
        raise HTTPException(status_code=403, detail="无权访问此房间")
    return room


async def get_active_room_for_user(user_id: str, db: AsyncSession) -> ActivityRoom | None:
    """Return the user's current active room for refresh recovery."""
    result = await db.execute(
        select(ActivityRoom)
        .where(ActivityRoom.user_id == user_id, ActivityRoom.status == "active")
        .order_by(ActivityRoom.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_room_snapshot(room_id: str, user_id: str, db: AsyncSession) -> dict[str, Any]:
    """Return full room state for reconnection."""
    room = await get_room(room_id, user_id, db)

    # Fetch messages summary
    result = await db.execute(
        select(RoomMessage)
        .where(RoomMessage.room_id == room_id)
        .order_by(RoomMessage.created_at.asc(), RoomMessage.id.asc())
    )
    messages = result.scalars().all()
    messages_data = [
        {
            "id": msg.id,
            "sender_type": msg.sender_type,
            "sender_id": msg.sender_id,
            "text": msg.text,
            "stage": msg.stage,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "generation": msg.generation_meta,
            "speech": msg.speech_meta,
        }
        for msg in messages
    ]

    public_state = _public_stage_state(room)

    # Dynamic mode: merge latest display fields from room_characters so the
    # snapshot reflects fallback name/era/mbti changes. After cleanup, the final
    # display fields retained in stage_state remain available.
    if get_settings().CHARACTER_GENERATION_MODE == "dynamic":
        char_result = await db.execute(
            select(RoomCharacter)
            .where(RoomCharacter.room_id == room_id)
            .execution_options(populate_existing=True)
        )
        char_by_id = {rc.id: rc for rc in char_result.scalars()}
        members = list(public_state.get("members") or [])
        for index, member in enumerate(members):
            if member.get("type") != "ai_peer":
                continue
            rc = char_by_id.get(member.get("id"))
            if rc is None:
                continue
            members[index] = {
                **member,
                "name": rc.name,
                "era": rc.era,
                "mbti": rc.mbti,
                "role": rc.role,
                "one_line_concept": rc.one_line_concept,
                "appearance_id": rc.appearance_id,
                "source": rc.source,
                "persona_status": rc.status,
            }
        public_state["members"] = members

    return {
        "room_id": room.id,
        "stage": room.current_stage,
        "stage_state": public_state,
        "status": room.status,
        "revision": room.revision,
        "messages": messages_data,
        "created_at": room.created_at.isoformat() if room.created_at else None,
    }


def _public_stage_state(room: ActivityRoom) -> dict[str, Any]:
    state = dict(room.stage_state or {})
    allowed = {
        "activity_type",
        "activity_version",
        "template_version",
        "members",
        "speaking_order",
        "painting_turn_index",
        "reflection_round_index",
        "reflection_turn_index",
        "reflection_rounds",
        "artwork_reveal_status",
        "facilitator",
        "contributions",
        "current_speaker_id",
        "generation_status",
        "generation_error",
        "image_status",
        "stage_deadline_at",
        "can_skip",
        "postcard_id",
        "crisis",
        "termination_reason",
        "final_prompt",
        "summary_text",
    }
    public = {key: value for key, value in state.items() if key in allowed}
    if state.get("artwork_path"):
        public["artwork_url"] = f"/api/rooms/{room.id}/artwork"
    else:
        public["artwork_url"] = None
    return public


async def advance_room_stage(
    room_id: str,
    target_stage: ActivityStage,
    idempotency_key: str | None,
    db: AsyncSession,
) -> dict[str, str]:
    """Acquire lock, validate, advance room stage, return new state."""
    async with room_lock_manager.acquire(room_id):
        result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = result.scalar_one_or_none()
        if room is None:
            raise HTTPException(status_code=404, detail="房间不存在")

        transition_result = advance_stage(room, target_stage, idempotency_key)

        # Update room status if terminal stage
        if target_stage == ActivityStage.COMPLETED:
            room.status = RoomStatus.COMPLETED.value
            room.ended_at = datetime.now(UTC)
            room_lock_manager.release(room_id)
        elif target_stage == ActivityStage.TERMINATED:
            room.status = RoomStatus.TERMINATED.value
            room.ended_at = datetime.now(UTC)
            room_lock_manager.release(room_id)

        await db.flush()
        return transition_result


async def terminate_room(room_id: str, reason: str, db: AsyncSession) -> ActivityRoom:
    """Set stage=TERMINATED, status=terminated, ended_at=now."""
    result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
    room = result.scalar_one_or_none()
    if room is None:
        raise HTTPException(status_code=404, detail="房间不存在")

    room.current_stage = ActivityStage.TERMINATED.value
    room.status = RoomStatus.TERMINATED.value
    room.ended_at = datetime.now(UTC)

    # Store termination reason in stage_state
    state = dict(room.stage_state) if room.stage_state else {}
    state["termination_reason"] = reason
    room.stage_state = state

    await db.flush()
    room_lock_manager.release(room_id)
    return room


async def get_active_room_count(db: AsyncSession) -> int:
    """Count rooms where status=active."""
    result = await db.execute(
        select(func.count())
        .select_from(ActivityRoom)
        .where(ActivityRoom.status == RoomStatus.ACTIVE.value)
    )
    return result.scalar_one()


async def update_heartbeat(room_id: str, db: AsyncSession) -> None:
    """Update heartbeat_at to now."""
    result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
    room = result.scalar_one_or_none()
    if room:
        room.heartbeat_at = datetime.now(UTC)
        await db.flush()


async def find_timed_out_rooms(timeout_minutes: int, db: AsyncSession) -> list[ActivityRoom]:
    """Find rooms where heartbeat_at < now - timeout."""
    cutoff = datetime.now(UTC) - timedelta(minutes=timeout_minutes)
    result = await db.execute(
        select(ActivityRoom).where(
            ActivityRoom.status == RoomStatus.ACTIVE.value,
            ActivityRoom.heartbeat_at < cutoff,
        )
    )
    return list(result.scalars().all())
