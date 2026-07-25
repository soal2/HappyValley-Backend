"""SQLAlchemy model for gratitude/three-good-things practice cards.

The card image itself is rendered client-side as a deterministic function of
(practice_type, content, feedback, event_location, created_at), so the backend
only persists the small metadata record — no image files are stored.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GratitudeCard(Base):
    __tablename__ = "gratitude_cards"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    # practice_type: "gratitude" | "three_good_things"
    practice_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 用户写下的正文（感恩信≤100字；三件好事为拼接后的全文≤120字）
    content: Mapped[str] = mapped_column(String(500), nullable=False)
    # AI 生成的温暖反馈
    feedback: Mapped[str] = mapped_column(String(200), nullable=False)
    # 活动时间地点（前端渲染用，如"感恩信·壁炉小屋"）
    event_location: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_gratitude_cards_user_id", "user_id"),)
