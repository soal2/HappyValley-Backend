"""Add meditation_cards table for meditation reward card metadata.

Revision ID: 003_meditation_cards
Revises: 002_remote_generation_contract
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003_meditation_cards"
down_revision: str | None = "002_remote_generation_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "meditation_cards",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("scene_key", sa.String(length=40), nullable=False),
        sa.Column("card_file", sa.String(length=120), nullable=False),
        sa.Column("quote", sa.String(length=200), nullable=False),
        sa.Column("nickname", sa.String(length=80), nullable=False),
        sa.Column("mbti", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_meditation_cards_user_id", "meditation_cards", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_meditation_cards_user_id", table_name="meditation_cards")
    op.drop_table("meditation_cards")
