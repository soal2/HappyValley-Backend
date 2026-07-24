"""Crisis service: message checking and crisis response handling."""

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.crisis import CrisisDetectionResult, CrisisDetector
from app.models.room import ActivityRoom, RoomMessage

logger = structlog.get_logger()

# Singleton detector instance
_detector: CrisisDetector | None = None


def _get_detector() -> CrisisDetector:
    """Get or create the singleton CrisisDetector."""
    global _detector
    if _detector is None:
        _detector = CrisisDetector(get_settings().CRISIS_CONFIG_PATH)
    return _detector


class CrisisService:
    """Handles crisis detection, response tracking, and timeout management."""

    @staticmethod
    async def check_message(
        text: str,
        room_id: str,
        user_id: str,
        db: AsyncSession,
        *,
        persist: bool = True,
    ) -> dict[str, Any] | None:
        """
        Run crisis detection on user message text.

        If triggered:
        - Do NOT store the original crisis text in room_messages
        - Store only a minimal record: {timestamp, user_id, triggered: true}
        - Return crisis alert data (aid message, options)

        If not triggered: return None
        """
        detector = _get_detector()
        result: CrisisDetectionResult | None = detector.detect(text)

        if result is None:
            return None

        logger.warning(
            "crisis_service.crisis_detected",
            room_id=room_id,
            user_id=user_id,
            severity=result.severity,
        )

        # Store minimal crisis record (no raw text for privacy/safety)
        if persist:
            crisis_record = RoomMessage(
                room_id=room_id,
                sender_type="system",
                sender_id="crisis_detector",
                text="[crisis_triggered]",
                stage="CRISIS",
                is_crisis=True,
            )
            db.add(crisis_record)
            await db.flush()

        # Return crisis alert data
        return {
            "crisis_triggered": True,
            "severity": result.severity,
            "aid_message": result.aid_message,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @staticmethod
    async def handle_crisis_response(
        room_id: str, user_id: str, can_continue: bool, db: AsyncSession
    ) -> dict[str, Any]:
        """
        Handle user's response to crisis alert.

        If can_continue=True: resume activity, record user_choice="continue"
        If can_continue=False: put room in CRISIS_WAITING state with 5-min countdown
        """
        result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = result.scalar_one_or_none()
        if room is None:
            raise ValueError(f"Room not found: {room_id}")

        stage_state = dict(room.stage_state) if room.stage_state else {}

        if can_continue:
            # User chose to continue
            stage_state["crisis_response"] = "continue"
            stage_state.pop("crisis_pause_at", None)
            room.stage_state = stage_state
            await db.flush()

            logger.info(
                "crisis_service.user_continues",
                room_id=room_id,
                user_id=user_id,
            )
            return {"status": "resumed", "room_id": room_id}
        else:
            # User chose to pause — start 5-minute countdown
            pause_time = datetime.now(UTC).isoformat()
            stage_state["crisis_response"] = "pause"
            stage_state["crisis_pause_at"] = pause_time
            room.stage_state = stage_state
            await db.flush()

            logger.info(
                "crisis_service.user_paused",
                room_id=room_id,
                user_id=user_id,
                pause_at=pause_time,
            )
            return {
                "status": "paused",
                "room_id": room_id,
                "timeout_seconds": 300,
                "pause_at": pause_time,
            }

    @staticmethod
    async def check_crisis_timeout(room_id: str, db: AsyncSession) -> dict[str, Any] | None:
        """
        Check if 5 minutes elapsed since crisis pause with no response.

        If timeout: terminate room, no postcard generated.
        Returns termination info or None if not timed out.
        """
        result = await db.execute(select(ActivityRoom).where(ActivityRoom.id == room_id))
        room = result.scalar_one_or_none()
        if room is None:
            return None

        stage_state = room.stage_state or {}
        crisis_pause_at = stage_state.get("crisis_pause_at")

        if not crisis_pause_at:
            return None

        pause_time = datetime.fromisoformat(crisis_pause_at)
        if pause_time.tzinfo is None:
            pause_time = pause_time.replace(tzinfo=UTC)

        elapsed = (datetime.now(UTC) - pause_time).total_seconds()

        if elapsed >= 300:  # 5 minutes
            # Terminate room — no postcard
            room.current_stage = "TERMINATED"
            room.status = "terminated"
            room.ended_at = datetime.now(UTC)

            state = dict(room.stage_state) if room.stage_state else {}
            state["termination_reason"] = "crisis_timeout"
            room.stage_state = state

            await db.flush()

            logger.info(
                "crisis_service.crisis_timeout_termination",
                room_id=room_id,
                elapsed_seconds=elapsed,
            )
            return {
                "terminated": True,
                "room_id": room_id,
                "reason": "crisis_timeout",
            }

        return None
