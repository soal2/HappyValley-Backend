"""SQLAlchemy model for AI characters."""

import uuid

from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    era: Mapped[str] = mapped_column(String(50), nullable=False)
    age_range_start: Mapped[int] = mapped_column(Integer, nullable=False)
    age_range_end: Mapped[int] = mapped_column(Integer, nullable=False)
    personality_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # "been-there"|"different-perspective"|"quiet-resonator"
    topics: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    speak_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_boundaries: Mapped[str | None] = mapped_column(Text, nullable=True)
    safety_fallback: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    fictional: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    appearance_id: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("name", "era", name="uq_character_name_era"),)
