"""Activity, queue, room, and command REST endpoints."""

from time import monotonic

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.database import get_db
from app.dependencies import get_current_user, require_profile_completed
from app.media.mime import image_media_type
from app.models.postcard import GeneratedImage
from app.models.user import User
from app.schemas.activity import (
    QueueStatusResponse,
    RoomCommandRequest,
    RoomCommandResponse,
    RoomSnapshotResponse,
    StartActivityRequest,
    StartActivityResponse,
)
from app.services import (
    activity_continuation_service,
    activity_service,
    queue_service,
    room_service,
)

router = APIRouter()
logger = structlog.get_logger()


@router.post("/api/activities", response_model=StartActivityResponse)
async def start_activity(
    request: StartActivityRequest,
    response: Response,
    user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> StartActivityResponse:
    active_room = await room_service.get_active_room_for_user(user.id, db)
    if active_room is not None:
        if str((active_room.stage_state or {}).get("activity_version")) != "2.0":
            await activity_service.terminate_legacy_room_for_upgrade(
                active_room, db, Container.get().media_storage
            )
            await queue_service.release_capacity(db)
            raise HTTPException(status_code=409, detail="flow_upgraded")
        return StartActivityResponse(status="started", room_id=active_room.id)
    claim, position = await queue_service.claim_or_queue(user.id, request.mood, db)
    if claim == "queued":
        response.status_code = status.HTTP_202_ACCEPTED
        return StartActivityResponse(status="queued", queue_position=position)
    mood = await queue_service.consume_reservation(user.id, db)
    room = await activity_service.create_activity_room(
        user, mood, db, Container.get().agent_runtime
    )
    response.status_code = status.HTTP_201_CREATED
    return StartActivityResponse(status="started", room_id=room.id)


@router.get("/api/queue/me", response_model=QueueStatusResponse)
async def queue_status(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> QueueStatusResponse:
    return QueueStatusResponse(**await queue_service.status_for(user.id, db))


@router.delete("/api/queue/me", status_code=204)
async def cancel_queue(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    await queue_service.cancel_for_user(user.id, db)


@router.post("/api/queue/confirm", response_model=StartActivityResponse)
async def confirm_queue(
    response: Response,
    user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> StartActivityResponse:
    mood = await queue_service.consume_reservation(user.id, db)
    room = await activity_service.create_activity_room(
        user, mood, db, Container.get().agent_runtime
    )
    response.status_code = status.HTTP_201_CREATED
    return StartActivityResponse(status="started", room_id=room.id)


@router.get("/api/rooms/active", response_model=RoomSnapshotResponse)
async def active_room_snapshot(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomSnapshotResponse:
    room = await room_service.get_active_room_for_user(user.id, db)
    if room is None:
        raise HTTPException(status_code=404, detail="当前没有进行中的活动")
    if str((room.stage_state or {}).get("activity_version")) != "2.0":
        await activity_service.terminate_legacy_room_for_upgrade(
            room, db, Container.get().media_storage
        )
        await queue_service.release_capacity(db)
        raise HTTPException(status_code=409, detail="flow_upgraded")
    snapshot = RoomSnapshotResponse(**await room_service.get_room_snapshot(room.id, user.id, db))
    if activity_continuation_service.needs_continuation(
        snapshot.status, snapshot.stage, snapshot.stage_state
    ):
        await activity_continuation_service.start(room.id)
    return snapshot


@router.get("/api/rooms/{room_id}", response_model=RoomSnapshotResponse)
async def room_snapshot(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomSnapshotResponse:
    room = await room_service.get_room(room_id, user.id, db)
    if room.status == "active" and str((room.stage_state or {}).get("activity_version")) != "2.0":
        await activity_service.terminate_legacy_room_for_upgrade(
            room, db, Container.get().media_storage
        )
        await queue_service.release_capacity(db)
        raise HTTPException(status_code=409, detail="flow_upgraded")
    snapshot = RoomSnapshotResponse(**await room_service.get_room_snapshot(room_id, user.id, db))
    if activity_continuation_service.needs_continuation(
        snapshot.status, snapshot.stage, snapshot.stage_state
    ):
        await activity_continuation_service.start(room_id)
    return snapshot


@router.get("/api/rooms/{room_id}/artwork")
async def room_artwork(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    room = await room_service.get_room(room_id, user.id, db)
    artwork_path = (room.stage_state or {}).get("artwork_path")
    if not artwork_path:
        raise HTTPException(status_code=404, detail="本活动尚无可读取的画作")
    image = await db.scalar(
        select(GeneratedImage)
        .where(GeneratedImage.room_id == room_id, GeneratedImage.file_path == artwork_path)
        .order_by(GeneratedImage.created_at.desc())
    )
    if image is None or image.status != "completed":
        raise HTTPException(status_code=404, detail="画作尚未生成完成")
    data = await Container.get().media_storage.read(str(artwork_path), user.id)
    return Response(data, media_type=image_media_type(data))


@router.post("/api/rooms/{room_id}/commands", response_model=RoomCommandResponse)
async def room_command(
    room_id: str,
    command: RoomCommandRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomCommandResponse:
    started_at = monotonic()
    container = Container.get()
    if command.type == "exit":
        await activity_continuation_service.cancel(room_id)
    result = await activity_service.execute_command(
        room_id,
        user.id,
        command.command_id,
        command.type,
        command.payload,
        db,
        container.agent_runtime,
        container.image_generator,
        container.media_storage,
    )
    snapshot = RoomSnapshotResponse(**await room_service.get_room_snapshot(room_id, user.id, db))
    if result.continuation_required:
        await activity_continuation_service.start(room_id, command.command_id)
    logger.info(
        "activity.command_confirmed",
        transport="http",
        room_id=room_id,
        command_id=command.command_id,
        command_type=command.type,
        revision=snapshot.revision,
        stage=snapshot.stage,
        duration_ms=round((monotonic() - started_at) * 1000),
        continuation_required=result.continuation_required,
    )
    return RoomCommandResponse(status=result.status, snapshot=snapshot)
