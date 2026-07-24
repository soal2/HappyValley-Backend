"""Activity, queue, room, and command REST endpoints."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.database import get_db
from app.dependencies import get_current_user, require_profile_completed
from app.models.user import User
from app.schemas.activity import (
    QueueStatusResponse,
    RoomCommandRequest,
    RoomCommandResponse,
    RoomSnapshotResponse,
    StartActivityRequest,
    StartActivityResponse,
)
from app.services import activity_service, queue_service, room_service

router = APIRouter()


@router.post("/api/activities", response_model=StartActivityResponse)
async def start_activity(
    request: StartActivityRequest,
    response: Response,
    user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> StartActivityResponse:
    claim, position = await queue_service.claim_or_queue(user.id, db)
    if claim == "queued":
        response.status_code = status.HTTP_202_ACCEPTED
        return StartActivityResponse(status="queued", queue_position=position)
    await queue_service.consume_reservation(user.id, db)
    room = await activity_service.create_activity_room(
        user, request.mood, db, Container.get().agent_runtime
    )
    response.status_code = status.HTTP_201_CREATED
    return StartActivityResponse(status="started", room_id=room.id)


@router.get("/api/queue/me", response_model=QueueStatusResponse)
async def queue_status(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> QueueStatusResponse:
    return QueueStatusResponse(**await queue_service.status_for(user.id, db))


@router.post("/api/queue/confirm", response_model=StartActivityResponse)
async def confirm_queue(
    response: Response,
    user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> StartActivityResponse:
    await queue_service.consume_reservation(user.id, db)
    room = await activity_service.create_activity_room(
        user, "等待后进入活动", db, Container.get().agent_runtime
    )
    response.status_code = status.HTTP_201_CREATED
    return StartActivityResponse(status="started", room_id=room.id)


@router.get("/api/rooms/{room_id}", response_model=RoomSnapshotResponse)
async def room_snapshot(
    room_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomSnapshotResponse:
    return RoomSnapshotResponse(**await room_service.get_room_snapshot(room_id, user.id, db))


@router.post("/api/rooms/{room_id}/commands", response_model=RoomCommandResponse)
async def room_command(
    room_id: str,
    command: RoomCommandRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RoomCommandResponse:
    container = Container.get()
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
    return RoomCommandResponse(status=result, snapshot=snapshot)
