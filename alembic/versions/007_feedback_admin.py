"""Add feedback tables and is_admin column on users.

Revision ID: 007_feedback_admin
Revises: 006_gratitude_cards
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007_feedback_admin"
down_revision: str | None = "006_gratitude_cards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add is_admin column to users
    op.add_column(
        "users",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # Feedback table (top-level messages + replies via parent_id self-reference)
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "parent_id",
            sa.String(length=36),
            sa.ForeignKey("feedback.id"),
            nullable=True,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])
    op.create_index("ix_feedback_parent_id", "feedback", ["parent_id"])

    # Feedback likes table (unique constraint on feedback_id + user_id)
    op.create_table(
        "feedback_likes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "feedback_id",
            sa.String(length=36),
            sa.ForeignKey("feedback.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("feedback_id", "user_id", name="uq_feedback_like"),
    )
    op.create_index("ix_feedback_likes_feedback_id", "feedback_likes", ["feedback_id"])


def downgrade() -> None:
    op.drop_index("ix_feedback_likes_feedback_id", table_name="feedback_likes")
    op.drop_table("feedback_likes")
    op.drop_index("ix_feedback_parent_id", table_name="feedback")
    op.drop_index("ix_feedback_user_id", table_name="feedback")
    op.drop_table("feedback")
    op.drop_column("users", "is_admin")
