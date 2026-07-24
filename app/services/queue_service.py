"""Single-instance capacity gate with durable FIFO queue and reservations."""

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.queue import QueueEntry
from app.models.room import ActivityRoom

capacity_lock = asyncio.Lock()


async def _expire_and_reserve(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    expired_result = await db.execute(
        select(QueueEntry).where(QueueEntry.status == "reserved", QueueEntry.expires_at < now)
    )
    for expired in expired_result.scalars():
        await db.delete(expired)

    active_count = await db.scalar(
        select(func.count()).select_from(ActivityRoom).where(ActivityRoom.status == "active")
    )
    reserved_count = await db.scalar(
        select(func.count()).select_from(QueueEntry).where(QueueEntry.status == "reserved")
    )
    available = max(
        0,
        get_settings().MAX_ACTIVE_ROOMS - int(active_count or 0) - int(reserved_count or 0),
    )
    if not available:
        return
    queued = await db.execute(
        select(QueueEntry)
        .where(QueueEntry.status == "queued")
        .order_by(QueueEntry.position, QueueEntry.created_at)
        .limit(available)
    )
    for entry in queued.scalars():
        entry.status = "reserved"
        entry.reserved_at = now
        entry.expires_at = now + timedelta(seconds=get_settings().RESERVATION_SECONDS)
    await db.flush()


async def has_occupancy(user_id: str, db: AsyncSession) -> bool:
    room = await db.scalar(
        select(ActivityRoom.id).where(
            ActivityRoom.user_id == user_id, ActivityRoom.status == "active"
        )
    )
    queue = await db.scalar(select(QueueEntry.id).where(QueueEntry.user_id == user_id))
    return room is not None or queue is not None


async def claim_or_queue(user_id: str, mood: str, db: AsyncSession) -> tuple[str, int | None]:
    """Return direct capacity or create exactly one FIFO entry."""
    async with capacity_lock:
        await _expire_and_reserve(db)
        if await has_occupancy(user_id, db):
            raise HTTPException(status_code=409, detail="同一账户只能排一次队或参加一个未结束活动")
        active_count = await db.scalar(
            select(func.count()).select_from(ActivityRoom).where(ActivityRoom.status == "active")
        )
        reserved_count = await db.scalar(
            select(func.count()).select_from(QueueEntry).where(QueueEntry.status == "reserved")
        )
        if int(active_count or 0) + int(reserved_count or 0) < get_settings().MAX_ACTIVE_ROOMS:
            now = datetime.now(UTC)
            db.add(
                QueueEntry(
                    user_id=user_id,
                    position=0,
                    status="reserved",
                    mood=mood,
                    reserved_at=now,
                    expires_at=now + timedelta(seconds=get_settings().RESERVATION_SECONDS),
                )
            )
            await db.commit()
            return "direct", None
        max_position = await db.scalar(select(func.coalesce(func.max(QueueEntry.position), 0)))
        entry = QueueEntry(
            user_id=user_id,
            position=int(max_position or 0) + 1,
            status="queued",
            mood=mood,
        )
        db.add(entry)
        await db.flush()
        position = await queue_position(entry, db)
        await db.commit()
        return "queued", position


async def queue_position(entry: QueueEntry, db: AsyncSession) -> int:
    if entry.status == "reserved":
        return 0
    count = await db.scalar(
        select(func.count())
        .select_from(QueueEntry)
        .where(QueueEntry.status == "queued", QueueEntry.position <= entry.position)
    )
    return int(count or 0)


async def status_for(user_id: str, db: AsyncSession) -> dict[str, object]:
    async with capacity_lock:
        await _expire_and_reserve(db)
        entry = await db.scalar(select(QueueEntry).where(QueueEntry.user_id == user_id))
        if entry is None:
            return {"status": "none", "position": None, "reservation_expires_at": None}
        return {
            "status": entry.status,
            "position": await queue_position(entry, db),
            "reservation_expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
        }


async def consume_reservation(user_id: str, db: AsyncSession) -> str:
    async with capacity_lock:
        await _expire_and_reserve(db)
        entry = await db.scalar(
            select(QueueEntry).where(QueueEntry.user_id == user_id, QueueEntry.status == "reserved")
        )
        if entry is None:
            raise HTTPException(status_code=409, detail="没有有效的预留席位")
        mood = entry.mood
        await db.delete(entry)
        await db.flush()
        return mood


async def release_capacity(db: AsyncSession) -> None:
    async with capacity_lock:
        await _expire_and_reserve(db)
        await db.commit()


async def cancel_for_user(user_id: str, db: AsyncSession) -> bool:
    """Remove a queued or reserved entry before a room is created."""
    async with capacity_lock:
        entry = await db.scalar(select(QueueEntry).where(QueueEntry.user_id == user_id))
        if entry is None:
            return False
        await db.delete(entry)
        await db.flush()
        await _expire_and_reserve(db)
        return True


async def maintain(db: AsyncSession) -> None:
    """Expire stale reservations and promote FIFO entries."""
    async with capacity_lock:
        await _expire_and_reserve(db)
