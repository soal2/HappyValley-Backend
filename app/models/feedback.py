"""SQLAlchemy models for feedback messages, replies, and likes."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("feedback.id"), nullable=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user = relationship("User", backref="feedbacks")
    replies: Mapped[list["Feedback"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
    )
    parent: Mapped["Feedback | None"] = relationship(
        back_populates="replies", remote_side="Feedback.id"
    )
    likes: Mapped[list["FeedbackLike"]] = relationship(
        back_populates="feedback", cascade="all, delete-orphan"
    )


class FeedbackLike(Base):
    __tablename__ = "feedback_likes"
    __table_args__ = (UniqueConstraint("feedback_id", "user_id", name="uq_feedback_like"),)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    feedback_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("feedback.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    feedback = relationship("Feedback", back_populates="likes")
