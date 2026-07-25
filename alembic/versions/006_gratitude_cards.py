"""Add gratitude_cards table for gratitude/three-good-things practice cards.

Revision ID: 006_gratitude_cards
Revises: 005_dynamic_room_characters
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006_gratitude_cards"
down_revision: str | None = "005_dynamic_room_characters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gratitude_cards",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("practice_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.String(length=500), nullable=False),
        sa.Column("feedback", sa.String(length=200), nullable=False),
        sa.Column("event_location", sa.String(length=80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_gratitude_cards_user_id", "gratitude_cards", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_gratitude_cards_user_id", table_name="gratitude_cards")
    op.drop_table("gratitude_cards")
