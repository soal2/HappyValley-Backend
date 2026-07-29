"""Idempotent background continuation for committed Painting House V2 commands."""

import asyncio
from time import monotonic

import structlog
from sqlalchemy import select

from app.container import Container
from app.database import async_session_factory
from app.domain.room_lock import room_lock_manager
from app.models.room import ActivityRoom
from app.realtime import connection_manager
from app.services import room_service

logger = structlog.get_logger()

_tasks: dict[str, asyncio.Task[None]] = {}
_tasks_lock = asyncio.Lock()


def needs_continuation(status: str, stage: str, state: dict[str, object]) -> bool:
    if status != "active" or str(state.get("activity_version")) != "2.0":
        return False
    if state.get("generation_status") == "failed":
        return False
    if state.get("generation_status") == "processing":
        return True
    if stage == "ARTWORK_REVEAL":
        return state.get("artwork_reveal_status") == "acknowledged"
    if stage in {"REFLECTION_QUESTION", "IMAGE_GENERATION", "AI_SUMMARY", "POSTCARD_GENERATION"}:
        return True
    if stage == "AI_INTRO":
        return bool(state.get("can_skip"))
    if stage not in {"PAINTING_TURN", "REFLECTION_TURN"}:
        return False
    current_id = state.get("current_speaker_id")
    members = state.get("members")
    if not isinstance(members, list):
        return False
    current = next(
        (
            member
            for member in members
            if isinstance(member, dict) and member.get("id") == current_id
        ),
        None,
    )
    return current is not None and current.get("type") != "human"


async def start(room_id: str, command_id: str | None = None) -> bool:
    """Start one continuation per room; return whether a new task was created."""
    async with _tasks_lock:
        existing = _tasks.get(room_id)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(_run(room_id, command_id))
        _tasks[room_id] = task
        return True


async def _run(room_id: str, command_id: str | None) -> None:
    from app.services import activity_service

    started_at = monotonic()
    logger.info(
        "activity.continuation_started",
        room_id=room_id,
        command_id=command_id,
    )
    try:
        async with async_session_factory() as db:
            owner_id = await db.scalar(
                select(ActivityRoom.user_id).where(
                    ActivityRoom.id == room_id, ActivityRoom.status == "active"
                )
            )
            if owner_id is None:
                return

            async def emit(event: dict[str, object]) -> None:
                await connection_manager.send_to_room(room_id, event)

            container = Container.get()
            await activity_service.resume_interrupted_activity(
                room_id,
                owner_id,
                db,
                container.agent_runtime,
                container.image_generator,
                container.media_storage,
                emit,
            )
        logger.info(
            "activity.continuation_completed",
            room_id=room_id,
            command_id=command_id,
            duration_ms=round((monotonic() - started_at) * 1000),
        )
    except asyncio.CancelledError:
        logger.info(
            "activity.continuation_cancelled",
            room_id=room_id,
            command_id=command_id,
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        raise
    except Exception as error:
        logger.exception(
            "activity.continuation_failed",
            room_id=room_id,
            command_id=command_id,
            duration_ms=round((monotonic() - started_at) * 1000),
        )
        async with room_lock_manager.acquire(room_id):
            async with async_session_factory() as db:
                room = await db.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
                if room is not None and room.status == "active":
                    state = dict(room.stage_state or {})
                    state.update(generation_status="failed", generation_error=str(error))
                    room.stage_state = state
                    room.revision += 1
                    await db.commit()
                    snapshot = await room_service.get_room_snapshot(room_id, room.user_id, db)
                    await connection_manager.send_to_room(
                        room_id,
                        {
                            "type": "error",
                            "error": "generation_failed",
                            "recoverable": True,
                            "data": snapshot,
                        },
                    )
    finally:
        current = asyncio.current_task()
        async with _tasks_lock:
            if _tasks.get(room_id) is current:
                _tasks.pop(room_id, None)


async def cancel(room_id: str) -> None:
    async with _tasks_lock:
        task = _tasks.get(room_id)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def cancel_all() -> None:
    async with _tasks_lock:
        tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    async with _tasks_lock:
        _tasks.clear()
