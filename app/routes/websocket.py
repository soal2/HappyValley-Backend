"""JWT-authenticated native WebSocket activity protocol."""

from datetime import UTC, datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from sqlalchemy import select

from app.container import Container
from app.database import async_session_factory
from app.models.room import ActivityRoom
from app.realtime import connection_manager
from app.schemas.activity import RoomCommandRequest
from app.services import activity_service, auth_service, queue_service, room_service

router = APIRouter()


async def _safe_send(websocket: WebSocket, payload: dict[str, object]) -> bool:
    try:
        await websocket.send_json(payload)
        return True
    except (RuntimeError, WebSocketDisconnect):
        return False


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
            container = Container.get()
            await activity_service.resume_interrupted_activity(
                room_id,
                user_id,
                db,
                container.agent_runtime,
                container.image_generator,
                container.media_storage,
            )
            snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
            if not await _safe_send(websocket, {"type": "snapshot", "data": snapshot}):
                return
        while True:
            raw = await websocket.receive_json()
            try:
                command = RoomCommandRequest.model_validate(raw)
            except ValidationError as error:
                await _safe_send(
                    websocket,
                    {"type": "error", "error": "invalid_command", "detail": error.errors()},
                )
                continue
            if not await _safe_send(
                websocket,
                {"type": "progress", "command_id": command.command_id, "status": "processing"},
            ):
                return
            async with async_session_factory() as db:
                container = Container.get()
                try:

                    async def emit(event: dict[str, object]) -> None:
                        await _safe_send(websocket, event)

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
                        emit,
                    )
                    await db.commit()
                    snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
                    if not await _safe_send(
                        websocket,
                        {
                            "type": "command_result",
                            "command_id": command.command_id,
                            "status": result,
                            "data": snapshot,
                        },
                    ):
                        return
                except Exception as error:
                    await db.rollback()
                    if not await _safe_send(
                        websocket,
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
