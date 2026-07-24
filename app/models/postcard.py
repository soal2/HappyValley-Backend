"""SQLAlchemy models for postcards and generated images."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Postcard(Base):
    __tablename__ = "postcards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    room_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("activity_rooms.id"), nullable=False, unique=True
    )
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    encouragement_text: Mapped[str] = mapped_column(Text, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    artwork_text: Mapped[str] = mapped_column(Text, nullable=False)
    generation_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    member_data: Mapped[list[dict[str, object]] | None] = mapped_column(JSON, nullable=True)
    template_version: Mapped[str] = mapped_column(String(20), nullable=False)
    share_token: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    shared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_postcards_user_id", "user_id"),
        Index("ix_postcards_share_token", "share_token"),
    )


class GeneratedImage(Base):
    __tablename__ = "generated_images"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    room_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("activity_rooms.id"), nullable=False
    )
    prompt_history: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # "pending"|"generating"|"completed"|"failed"
    generation_meta: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
