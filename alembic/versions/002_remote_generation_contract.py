"""Persist queue mood and safe remote-generation provenance.

Revision ID: 002_remote_generation_contract
Revises: 001_initial_schema
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_remote_generation_contract"
down_revision: str | None = "001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("queue_entries", sa.Column("mood", sa.Text(), nullable=True))
    op.execute("UPDATE queue_entries SET mood = '等待后进入活动' WHERE mood IS NULL")
    op.alter_column("queue_entries", "mood", nullable=False)
    op.add_column("room_messages", sa.Column("generation_meta", sa.JSON(), nullable=True))
    op.add_column("generated_images", sa.Column("generation_meta", sa.JSON(), nullable=True))
    op.add_column("postcards", sa.Column("generation_meta", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("postcards", "generation_meta")
    op.drop_column("generated_images", "generation_meta")
    op.drop_column("room_messages", "generation_meta")
    op.drop_column("queue_entries", "mood")
