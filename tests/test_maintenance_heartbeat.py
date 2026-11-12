"""维护循环心跳兜底回收的回归测试（P0-8：僵尸房间永久占用容量）。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import async_session_factory
from app.models.room import ActivityRoom
from app.realtime import connection_manager
from app.services import auth_service, queue_service
from app.services.maintenance_service import maintain_runtime_state


async def _make_room(
    username: str,
    *,
    heartbeat_age_seconds: float,
    disconnected_age_seconds: float | None = None,
) -> str:
    now = datetime.now(UTC)
    async with async_session_factory() as db:
        user = await auth_service.register(username, "safe-password", db)
        room = ActivityRoom(
            user_id=user.id,
            status="active",
            current_stage="PAINTING_TURN",
            stage_state={"activity_version": "2.0"},
            heartbeat_at=now - timedelta(seconds=heartbeat_age_seconds),
            disconnected_at=(
                now - timedelta(seconds=disconnected_age_seconds)
                if disconnected_age_seconds is not None
                else None
            ),
        )
        db.add(room)
        await db.commit()
        return room.id


async def _room_status(room_id: str) -> tuple[str, dict]:
    async with async_session_factory() as db:
        room = await db.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
        assert room is not None
        return room.status, dict(room.stage_state or {})


async def test_heartbeat_timeout_terminates_zombie_room() -> None:
    """无 WS 断开记录、长时间无心跳的活跃房间必须被回收。"""
    room_id = await _make_room("zombie", heartbeat_age_seconds=660)
    async with async_session_factory() as db:
        terminated = await maintain_runtime_state(db)
        await db.commit()
    assert terminated >= 1
    status, state = await _room_status(room_id)
    assert status == "terminated"
    assert state.get("termination_reason") == "heartbeat_timeout"


async def test_fresh_heartbeat_keeps_room_active() -> None:
    room_id = await _make_room("alive", heartbeat_age_seconds=10)
    async with async_session_factory() as db:
        await maintain_runtime_state(db)
        await db.commit()
    status, _ = await _room_status(room_id)
    assert status == "active"


async def test_live_ws_connection_exempts_stale_heartbeat() -> None:
    """存活 WS 连接豁免心跳回收（防止误杀在线用户）。"""
    room_id = await _make_room("ws_alive", heartbeat_age_seconds=660)
    connection_manager._connections[room_id] = object()  # type: ignore[assignment]
    try:
        async with async_session_factory() as db:
            await maintain_runtime_state(db)
            await db.commit()
        status, _ = await _room_status(room_id)
        assert status == "active"
    finally:
        connection_manager._connections.pop(room_id, None)


async def test_reconnect_timeout_still_terminates() -> None:
    """既有重连窗口回收行为不受影响。"""
    room_id = await _make_room(
        "disconnected", heartbeat_age_seconds=10, disconnected_age_seconds=360
    )
    async with async_session_factory() as db:
        terminated = await maintain_runtime_state(db)
        await db.commit()
    assert terminated >= 1
    status, state = await _room_status(room_id)
    assert status == "terminated"
    assert state.get("termination_reason") == "reconnect_timeout"


async def test_capacity_freed_for_queued_user_after_zombie_cleanup() -> None:
    """僵尸房间回收后，排队用户应能立即获得席位（事故核心诉求）。"""
    for i in range(6):
        await _make_room(f"zombie_capacity_{i}", heartbeat_age_seconds=660)
    async with async_session_factory() as db:
        waiter = await auth_service.register("waiter", "safe-password", db)
        await db.commit()
    async with async_session_factory() as db:
        claim, position = await queue_service.claim_or_queue(waiter.id, "平静", db)
    assert claim == "queued"
    assert position == 1

    async with async_session_factory() as db:
        await maintain_runtime_state(db)
        await db.commit()

    async with async_session_factory() as db:
        status = await queue_service.status_for(waiter.id, db)
    assert status["status"] == "reserved"
