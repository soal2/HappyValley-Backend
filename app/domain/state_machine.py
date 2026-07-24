"""Activity state machine: validates and performs stage transitions."""

from datetime import UTC, datetime

from fastapi import HTTPException

from app.domain.activity_states import STAGE_TRANSITIONS, ActivityStage
from app.models.room import ActivityRoom


def validate_transition(current_stage: ActivityStage, target_stage: ActivityStage) -> None:
    """Check that a transition from current to target is valid. Raises 400 if not."""
    valid_targets = STAGE_TRANSITIONS.get(current_stage, [])
    if target_stage not in valid_targets:
        raise HTTPException(
            status_code=400,
            detail=f"无效的阶段转换: {current_stage.value} → {target_stage.value}",
        )


def get_next_stage(current_stage: ActivityStage) -> ActivityStage | None:
    """Return the natural next stage (first in transition list, excluding TERMINATED)."""
    valid_targets = STAGE_TRANSITIONS.get(current_stage, [])
    for target in valid_targets:
        if target != ActivityStage.TERMINATED:
            return target
    return None


def can_advance(room: ActivityRoom) -> bool:
    """Check if preconditions for next stage are met."""
    current = ActivityStage(room.current_stage)
    next_stage = get_next_stage(current)
    return next_stage is not None


def advance_stage(
    room: ActivityRoom,
    target_stage: ActivityStage,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    """
    Advance room to target stage with optional idempotency.

    Returns:
        dict with 'status': 'advanced' | 'already_processed'
    """
    current = ActivityStage(room.current_stage)
    validate_transition(current, target_stage)

    # Initialize stage_state if needed
    if room.stage_state is None:
        room.stage_state = {}

    # Check idempotency
    processed_keys = room.stage_state.get("processed_keys", [])
    if idempotency_key and idempotency_key in processed_keys:
        return {"status": "already_processed", "stage": room.current_stage}

    # Perform transition
    room.current_stage = target_stage.value
    now_str = datetime.now(UTC).isoformat()

    # Store transition metadata
    new_state = dict(room.stage_state) if room.stage_state else {}
    new_state["last_transition_at"] = now_str
    new_state["previous_stage"] = current.value

    if idempotency_key:
        existing_keys = new_state.get("processed_keys", [])
        existing_keys.append(idempotency_key)
        new_state["processed_keys"] = existing_keys

    room.stage_state = new_state

    return {"status": "advanced", "stage": target_stage.value}
