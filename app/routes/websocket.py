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
from app.services import activity_service, auth_service, room_service

router = APIRouter()


@router.websocket("/api/rooms/{room_id}/ws")
async def room_websocket(websocket: WebSocket, room_id: str) -> None:
    token = websocket.query_params.get("token")
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
        await connection_manager.connect(room_id, websocket)
        room.disconnected_at = None
        room.heartbeat_at = datetime.now(UTC)
        await db.commit()

    try:
        async with async_session_factory() as db:
            snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
            await websocket.send_json({"type": "snapshot", "data": snapshot})
        while True:
            raw = await websocket.receive_json()
            try:
                command = RoomCommandRequest.model_validate(raw)
            except ValidationError as error:
                await websocket.send_json(
                    {"type": "error", "error": "invalid_command", "detail": error.errors()}
                )
                continue
            await websocket.send_json(
                {"type": "progress", "command_id": command.command_id, "status": "processing"}
            )
            async with async_session_factory() as db:
                container = Container.get()
                try:
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
                    )
                    await db.commit()
                    snapshot = await room_service.get_room_snapshot(room_id, user_id, db)
                    await websocket.send_json(
                        {
                            "type": "command_result",
                            "command_id": command.command_id,
                            "status": result,
                            "data": snapshot,
                        }
                    )
                except Exception as error:
                    await db.rollback()
                    await websocket.send_json(
                        {
                            "type": "error",
                            "command_id": command.command_id,
                            "error": "command_rejected",
                            "detail": str(error),
                        }
                    )
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
