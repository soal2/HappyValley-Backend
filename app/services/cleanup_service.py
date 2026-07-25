"""Cleanup service: atomic data deletion after postcard or termination."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.media.storage import MediaStorage
from app.models.postcard import GeneratedImage
from app.models.room import ActivityRoom, RoomMessage
from app.models.room_character import RoomCharacter

logger = structlog.get_logger()


class CleanupService:
    """Atomic data cleanup for completed or terminated rooms."""

    @staticmethod
    async def cleanup_after_postcard(room_id: str, db: AsyncSession) -> dict[str, Any]:
        """
        Cleanup after postcard generation — within a single transaction.

        Steps (atomic):
        1. Delete ALL room_messages for this room_id
        2. Clear stage_state to minimal metadata only (keep duration, final_stage)
        3. Delete GeneratedImage records marked as intermediate (keep final one)
        4. Update room status to "completed", set ended_at

        Returns cleanup summary.
        """
        # V2 keeps the committed transcript and cursors so completed rooms can be
        # restored after a refresh. Legacy rooms retain the original destructive
        # cleanup behavior.
        # Room character personas (sensitive data) are always deleted regardless
        # of activity version — they exist only to serve the live relay.
        char_result = await db.execute(
            delete(RoomCharacter).where(RoomCharacter.room_id == room_id)
        )
        characters_deleted = char_result.rowcount  # type: ignore[attr-defined]
        result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = result.scalar_one_or_none()
        if room is None:
            raise ValueError(f"Room not found: {room_id}")

        old_state = room.stage_state or {}
        if str(old_state.get("activity_version")) == "2.0":
            messages_deleted = 0
            retained_state = dict(old_state)
            retained_state.pop("mood", None)
            retained_state.pop("crisis", None)
            retained_state.update(
                final_stage=room.current_stage,
                cleanup_at=datetime.now(UTC).isoformat(),
                generation_status="ready",
                generation_error=None,
                current_speaker_id=None,
            )
            room.stage_state = retained_state
        else:
            msg_result = await db.execute(delete(RoomMessage).where(RoomMessage.room_id == room_id))
            messages_deleted = msg_result.rowcount  # type: ignore[attr-defined]
            room.stage_state = {
                "duration_seconds": old_state.get("duration_seconds"),
                "final_stage": room.current_stage,
                "cleanup_at": datetime.now(UTC).isoformat(),
                "activity_version": old_state.get("activity_version"),
                "template_version": old_state.get("template_version"),
                "postcard_id": old_state.get("postcard_id"),
                "processed_command_ids": old_state.get("processed_command_ids", []),
                "members": old_state.get("members", []),
                "speaking_order": old_state.get("speaking_order", []),
                "facilitator": old_state.get("facilitator"),
                "artwork_path": old_state.get("artwork_path"),
                "image_status": old_state.get("image_status"),
                "generation_status": "ready",
                "current_speaker_id": None,
            }

        # 3. Delete intermediate GeneratedImage records (keep the final completed one)
        # Find the final image (most recent completed)
        image_result = await db.execute(
            select(GeneratedImage)
            .where(
                GeneratedImage.room_id == room_id,
                GeneratedImage.status == "completed",
            )
            .order_by(GeneratedImage.created_at.desc())
            .limit(1)
        )
        final_image = image_result.scalar_one_or_none()
        final_image_id = final_image.id if final_image else None
        if final_image:
            await db.execute(
                update(GeneratedImage)
                .where(GeneratedImage.id == final_image.id)
                .values(prompt_history=null())
            )

        # Delete all non-final images
        if final_image_id:
            img_result = await db.execute(
                delete(GeneratedImage).where(
                    GeneratedImage.room_id == room_id,
                    GeneratedImage.id != final_image_id,
                )
            )
        else:
            img_result = await db.execute(
                delete(GeneratedImage).where(GeneratedImage.room_id == room_id)
            )
        images_deleted = img_result.rowcount  # type: ignore[attr-defined]

        # 4. Update room status
        room.status = "completed"
        room.ended_at = datetime.now(UTC)

        await db.flush()

        summary = {
            "room_id": room_id,
            "messages_deleted": messages_deleted,
            "characters_deleted": characters_deleted,
            "images_deleted": images_deleted,
            "final_image_kept": final_image_id,
            "status": "completed",
        }
        logger.info("cleanup_service.postcard_cleanup_done", **summary)
        return summary

    @staticmethod
    async def cleanup_terminated_room(
        room_id: str, db: AsyncSession, media_storage: MediaStorage | None = None
    ) -> dict[str, Any]:
        """
        Cleanup for terminated room — no postcard generated.

        Steps (atomic):
        1. Delete all room_messages
        2. Clear stage_state
        3. Delete ALL GeneratedImage records (no postcard)
        4. Update room status to "terminated", set ended_at
        """
        # 1. Delete all room messages
        msg_result = await db.execute(delete(RoomMessage).where(RoomMessage.room_id == room_id))
        messages_deleted = msg_result.rowcount  # type: ignore[attr-defined]

        # 1b. Delete all room character personas (sensitive persona data)
        char_result = await db.execute(
            delete(RoomCharacter).where(RoomCharacter.room_id == room_id)
        )
        characters_deleted = char_result.rowcount  # type: ignore[attr-defined]

        # 2. Clear stage_state
        room_result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = room_result.scalar_one_or_none()
        if room is None:
            raise ValueError(f"Room not found: {room_id}")

        room.stage_state = {
            "final_stage": room.current_stage,
            "cleanup_at": datetime.now(UTC).isoformat(),
            "termination_reason": (room.stage_state or {}).get("termination_reason", "unknown"),
        }

        # 3. Delete ALL generated media and metadata
        if media_storage:
            image_rows = await db.execute(
                select(GeneratedImage).where(GeneratedImage.room_id == room_id)
            )
            for image in image_rows.scalars():
                if image.file_path:
                    await media_storage.delete(image.file_path)
        img_result = await db.execute(
            delete(GeneratedImage).where(GeneratedImage.room_id == room_id)
        )
        images_deleted = img_result.rowcount  # type: ignore[attr-defined]

        # 4. Update room status
        room.status = "terminated"
        room.current_stage = "TERMINATED"
        room.ended_at = datetime.now(UTC)

        await db.flush()

        summary = {
            "room_id": room_id,
            "messages_deleted": messages_deleted,
            "characters_deleted": characters_deleted,
            "images_deleted": images_deleted,
            "status": "terminated",
        }
        logger.info("cleanup_service.terminated_cleanup_done", **summary)
        return summary

    @staticmethod
    async def verify_cleanup(room_id: str, db: AsyncSession) -> dict[str, Any]:
        """
        Verify cleanup was successful — for testing.

        Asserts:
        - No messages remain for this room
        - stage_state has no sensitive content
        """
        # Check no messages remain
        messages_result = await db.execute(
            select(RoomMessage).where(RoomMessage.room_id == room_id)
        )
        remaining_messages = messages_result.scalars().all()

        # Check no room character personas remain
        characters_result = await db.execute(
            select(RoomCharacter).where(RoomCharacter.room_id == room_id)
        )
        remaining_characters = characters_result.scalars().all()

        # Check stage_state
        room_result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = room_result.scalar_one_or_none()

        stage_state = room.stage_state if room else {}
        # Sensitive keys that should NOT be in stage_state after cleanup
        sensitive_keys = [
            "user_sharing_text",
            "crisis_pause_at",
            "member_data",
            "transcript",
        ]
        has_sensitive = [k for k in sensitive_keys if k in (stage_state or {})]

        verification = {
            "room_id": room_id,
            "messages_remaining": len(remaining_messages),
            "messages_clean": len(remaining_messages) == 0,
            "characters_remaining": len(remaining_characters),
            "characters_clean": len(remaining_characters) == 0,
            "sensitive_keys_found": has_sensitive,
            "state_clean": len(has_sensitive) == 0,
            "overall_clean": (
                len(remaining_messages) == 0
                and len(remaining_characters) == 0
                and len(has_sensitive) == 0
            ),
        }
        return verification
