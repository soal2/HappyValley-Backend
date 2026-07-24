"""Initial P0 schema.

Revision ID: 001_initial_schema
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "user_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("mbti", sa.String(10)),
        sa.Column("gender", sa.String(20)),
        sa.Column("birth_year", sa.Integer()),
        sa.Column("appearance_id", sa.String(100)),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "characters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("era", sa.String(50), nullable=False),
        sa.Column("age_range_start", sa.Integer(), nullable=False),
        sa.Column("age_range_end", sa.Integer(), nullable=False),
        sa.Column("personality_type", sa.String(30), nullable=False),
        sa.Column("topics", sa.JSON()),
        sa.Column("speak_style", sa.Text()),
        sa.Column("facts_boundaries", sa.Text()),
        sa.Column("safety_fallback", sa.Text()),
        sa.Column("system_prompt_template", sa.Text()),
        sa.Column("config_version", sa.String(20)),
        sa.Column("fictional", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("appearance_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("name", "era", name="uq_character_name_era"),
    )
    op.create_table(
        "activity_rooms",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_stage", sa.String(50), nullable=False),
        sa.Column("stage_state", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("disconnected_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_activity_rooms_user_status", "activity_rooms", ["user_id", "status"])
    op.create_index(
        "ix_activity_rooms_status_heartbeat", "activity_rooms", ["status", "heartbeat_at"]
    )
    op.create_index(
        "uq_one_active_room_per_user",
        "activity_rooms",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "room_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("room_id", sa.String(36), sa.ForeignKey("activity_rooms.id"), nullable=False),
        sa.Column("sender_type", sa.String(10), nullable=False),
        sa.Column("sender_id", sa.String(100), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("stage", sa.String(50), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("is_crisis", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index("ix_room_messages_room_created", "room_messages", ["room_id", "created_at"])
    op.create_table(
        "generated_images",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("room_id", sa.String(36), sa.ForeignKey("activity_rooms.id"), nullable=False),
        sa.Column("prompt_history", sa.JSON()),
        sa.Column("file_path", sa.String(500)),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "postcards",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "room_id",
            sa.String(36),
            sa.ForeignKey("activity_rooms.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("image_path", sa.String(500)),
        sa.Column("encouragement_text", sa.Text(), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("artwork_text", sa.Text(), nullable=False),
        sa.Column("member_data", sa.JSON()),
        sa.Column("template_version", sa.String(20), nullable=False),
        sa.Column("share_token", sa.String(100), unique=True),
        sa.Column("shared_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_postcards_user_id", "postcards", ["user_id"])
    op.create_index("ix_postcards_share_token", "postcards", ["share_token"])
    op.create_table(
        "queue_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_queue_entries_status_position", "queue_entries", ["status", "position"])


def downgrade() -> None:
    op.drop_table("queue_entries")
    op.drop_table("postcards")
    op.drop_table("generated_images")
    op.drop_table("room_messages")
    op.drop_table("activity_rooms")
    op.drop_table("characters")
    op.drop_table("user_profiles")
    op.drop_table("users")
