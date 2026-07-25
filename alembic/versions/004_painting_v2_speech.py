"""Persist server-approved painting activity speech chunks.

Revision ID: 004_painting_v2_speech
Revises: 003_meditation_cards
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004_painting_v2_speech"
down_revision: str | None = "003_meditation_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("room_messages", sa.Column("speech_meta", sa.JSON(), nullable=True))
    op.add_column("characters", sa.Column("mbti", sa.String(length=4), nullable=True))


def downgrade() -> None:
    op.drop_column("characters", "mbti")
    op.drop_column("room_messages", "speech_meta")
