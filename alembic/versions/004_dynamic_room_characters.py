"""Add room_characters table for dynamic teammate generation.

Revision ID: 005_dynamic_room_characters
Revises: 004_painting_v2_speech

Note: characters.mbti was already added by 004_painting_v2_speech (String(4),
sufficient for 4-letter MBTI types). This migration only creates the
room_characters table used to store per-room generated AI teammate personas.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005_dynamic_room_characters"
down_revision: str | None = "004_painting_v2_speech"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "room_characters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "room_id",
            sa.String(length=36),
            sa.ForeignKey("activity_rooms.id"),
            nullable=False,
        ),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("era", sa.String(length=50), nullable=False),
        sa.Column("mbti", sa.String(length=8), nullable=False),
        sa.Column("appearance_id", sa.String(length=100), nullable=False),
        sa.Column("system_prompt_template", sa.Text(), nullable=True),
        sa.Column("speak_style", sa.Text(), nullable=True),
        sa.Column("facts_boundaries", sa.Text(), nullable=True),
        sa.Column("safety_fallback", sa.Text(), nullable=True),
        sa.Column("topics", sa.JSON(), nullable=True),
        sa.Column("five_dim", sa.JSON(), nullable=True),
        sa.Column("one_line_concept", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("generation_meta", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_room_characters_room_slot", "room_characters", ["room_id", "slot"])
    op.create_index(
        "ix_room_characters_status_updated", "room_characters", ["status", "updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_room_characters_status_updated", table_name="room_characters")
    op.drop_index("ix_room_characters_room_slot", table_name="room_characters")
    op.drop_table("room_characters")
