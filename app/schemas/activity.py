"""Public activity, queue, room, and command contracts."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class StartActivityRequest(BaseModel):
    mood: str = Field(min_length=1, max_length=1000)


class StartActivityResponse(BaseModel):
    status: Literal["started", "queued"]
    room_id: str | None = None
    queue_position: int | None = None


class QueueStatusResponse(BaseModel):
    status: Literal["none", "queued", "reserved"]
    position: int | None = None
    reservation_expires_at: str | None = None


class RoomSnapshotResponse(BaseModel):
    room_id: str
    stage: str
    stage_state: dict[str, Any]
    status: str
    revision: int
    messages: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None


class RoomCommandRequest(BaseModel):
    command_id: str = Field(min_length=1, max_length=100)
    type: Literal[
        "submit_base",
        "submit_sharing",
        "submit_painting_turn",
        "complete_artwork_reveal",
        "submit_reflection",
        "skip_reflection",
        "answer_follow_up",
        "skip_follow_up",
        "skip_current_stage",
        "retry_current_stage",
        "retry_image",
        "crisis_response",
        "heartbeat",
        "exit",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class RoomCommandResponse(BaseModel):
    status: Literal["accepted", "already_processed"]
    snapshot: RoomSnapshotResponse
