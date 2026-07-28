"""JWT-authenticated native WebSocket activity protocol."""

from datetime import UTC, datetime
from time import monotonic

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from app.container import Container
from app.database import async_session_factory
from app.models.room import ActivityRoom
from app.realtime import connection_manager
from app.schemas.activity import RoomCommandRequest
from app.services import (
    activity_continuation_service,
    activity_service,
    auth_service,
    queue_service,
    room_service,
)

router = APIRouter()
logger = structlog.get_logger()


@router.websocket("/api/rooms/{room_id}/ws")
async def room_websocket(websocket: WebSocket, room_id: str) -> None:
    requested_protocols = [
        item.strip()
        for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if item.strip()
    ]
    protocol_token = (
        requested_protocols[1]
        if len(requested_protocols) == 2 and requested_protocols[0] == "happyvalley.jwt"
        else None
    )
    token = protocol_token or websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4401, reason="缺少认证令牌")
        return
    try:
        payload = auth_service.verify_token(token)
    except Exception:
        await websocket.close(code=4401, reason="无效的认证令牌")
        return
    user_id = str(payload["sub"])
    async with async_session_factory() as db:
        room = await db.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
        if room is None:
            await websocket.close(code=4404, reason="房间不存在")
            return
        if room.user_id != user_id:
            await websocket.close(code=4403, reason="无权访问此房间")
            return
        if (
            room.status == "active"
            and str((room.stage_state or {}).get("activity_version")) != "2.0"
        ):
            await activity_service.terminate_legacy_room_for_upgrade(
                room, db, Container.get().media_storage
            )
            await queue_service.release_capacity(db)
            await websocket.close(code=4410, reason="flow_upgraded")
            return
        await connection_manager.connect(
            room_id,
            websocket,
            subprotocol="happyvalley.jwt" if protocol_token else None,
        )
        room.disconnected_at = None
        room.heartbeat_at = datetime.now(UTC)
        await db.commit()

    try:
        async with async_session_factory() as db:
            snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
            if not await connection_manager.send_to_room(
                room_id, {"type": "snapshot", "data": snapshot}
            ):
                return
            if activity_continuation_service.needs_continuation(
                snapshot["status"], snapshot["stage"], snapshot["stage_state"]
            ):
                await activity_continuation_service.start(room_id)
        while True:
            raw = await websocket.receive_json()
            try:
                command = RoomCommandRequest.model_validate(raw)
            except ValidationError as error:
                await connection_manager.send_to_room(
                    room_id,
                    {"type": "error", "error": "invalid_command", "detail": error.errors()},
                )
                continue
            if not await connection_manager.send_to_room(
                room_id,
                {"type": "progress", "command_id": command.command_id, "status": "processing"},
            ):
                return
            async with async_session_factory() as db:
                container = Container.get()
                started_at = monotonic()
                try:
                    if command.type == "exit":
                        await activity_continuation_service.cancel(room_id)

                    result = await activity_service.execute_command(
                        room_id,
                        user_id,
                        command.command_id,
                        command.type,
                        command.payload,
                        db,
                        container.agent_runtime,
                        container.image_generator,
                        container.media_storage,
                        None,
                    )
                    await db.commit()
                    snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
                    if not await connection_manager.send_to_room(
                        room_id,
                        {
                            "type": "command_result",
                            "command_id": command.command_id,
                            "status": result.status,
                            "data": snapshot,
                        },
                    ):
                        return
                    if result.continuation_required:
                        await activity_continuation_service.start(room_id, command.command_id)
                    logger.info(
                        "activity.command_confirmed",
                        transport="websocket",
                        room_id=room_id,
                        command_id=command.command_id,
                        command_type=command.type,
                        revision=snapshot["revision"],
                        stage=snapshot["stage"],
                        duration_ms=round((monotonic() - started_at) * 1000),
                        continuation_required=result.continuation_required,
                    )
                except Exception as error:
                    await db.rollback()
                    if not await connection_manager.send_to_room(
                        room_id,
                        {
                            "type": "error",
                            "command_id": command.command_id,
                            "error": "command_rejected",
                            "detail": str(error),
                            "recoverable": True,
                        },
                    ):
                        return
    except WebSocketDisconnect:
        pass
    finally:
        is_current = await connection_manager.disconnect(room_id, websocket)
        if is_current:
            async with async_session_factory() as db:
                room = await db.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
                if room is not None and room.status == "active":
                    room.disconnected_at = datetime.now(UTC)
                    await db.commit()
