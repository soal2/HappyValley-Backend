"""SQLAlchemy model for meditation reward cards.

The card image itself is a deterministic function of
(card_file, quote, nickname, mbti) rendered client-side, so the backend
only persists the small metadata record — no image files are stored.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MeditationCard(Base):
    __tablename__ = "meditation_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    scene_key: Mapped[str] = mapped_column(String(40), nullable=False)
    card_file: Mapped[str] = mapped_column(String(120), nullable=False)
    quote: Mapped[str] = mapped_column(String(200), nullable=False)
    nickname: Mapped[str] = mapped_column(String(80), nullable=False)
    mbti: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_meditation_cards_user_id", "user_id"),)
