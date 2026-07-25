"""SQLAlchemy model for per-room generated AI teammate personas."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RoomCharacter(Base):
    """A single AI teammate slot bound to an activity room.

    Holds the authoritative persona (system_prompt_template, speak_style,
    facts_boundaries, safety_fallback, topics) for the room's relay. Real
    teammates snapshot their persona from the whitelist ``Character`` row at
    selection time; conceptual teammates are filled in by the background
    generation task; fallback rows copy from a reserve character. The ``id``
    is stable and used as ``RoomMessage.sender_id`` — it never changes even
    when a conceptual row falls back to a reserve persona.
    """

    __tablename__ = "room_characters"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    room_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("activity_rooms.id"), nullable=False
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # real|conceptual|fallback
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    era: Mapped[str] = mapped_column(String(50), nullable=False)
    mbti: Mapped[str] = mapped_column(String(8), nullable=False)
    appearance_id: Mapped[str] = mapped_column(String(100), nullable=False)
    system_prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    speak_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_boundaries: Mapped[str | None] = mapped_column(Text, nullable=True)
    safety_fallback: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    five_dim: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    one_line_concept: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="skeleton", nullable=False
    )  # skeleton|generating|ready|failed
    generation_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_room_characters_room_slot", "room_id", "slot"),
        Index("ix_room_characters_status_updated", "status", "updated_at"),
    )
