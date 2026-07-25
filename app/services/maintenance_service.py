"""In-process recovery for stale rooms and durable queue reservations."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.container import Container
from app.models.room import ActivityRoom
from app.services import character_generation_service, queue_service
from app.services.cleanup_service import CleanupService


async def maintain_runtime_state(db: AsyncSession, *, startup: bool = False) -> int:
    """Reconcile state after restart and terminate expired reconnect windows."""
    now = datetime.now(UTC)
    if startup:
        active_result = await db.execute(
            select(ActivityRoom).where(
                ActivityRoom.status == "active", ActivityRoom.disconnected_at.is_(None)
            )
        )
        for room in active_result.scalars():
            room.disconnected_at = now

    cutoff = now - timedelta(seconds=get_settings().RECONNECT_SECONDS)
    stale_result = await db.execute(
        select(ActivityRoom).where(
            ActivityRoom.status == "active", ActivityRoom.disconnected_at < cutoff
        )
    )
    terminated = 0
    for room in stale_result.scalars():
        state = dict(room.stage_state or {})
        state["termination_reason"] = "reconnect_timeout"
        room.stage_state = state
        await CleanupService.cleanup_terminated_room(room.id, db, Container.get().media_storage)
        terminated += 1

    # Recover room_characters stuck in skeleton/generating past the deadline
    # (e.g. after a process restart killed the background Stage B task).
    await character_generation_service.cleanup_stale_generations(db)

    await queue_service.maintain(db)
    return terminated
