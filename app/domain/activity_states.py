"""Activity stage definitions and valid transitions."""

from enum import StrEnum


class ActivityStage(StrEnum):
    WAITING = "WAITING"
    AI_INTRO = "AI_INTRO"
    PAINTING_TURN = "PAINTING_TURN"
    ARTWORK_REVEAL = "ARTWORK_REVEAL"
    REFLECTION_QUESTION = "REFLECTION_QUESTION"
    REFLECTION_TURN = "REFLECTION_TURN"
    HUMAN_BASE_PAINTING = "HUMAN_BASE_PAINTING"
    AI_TURN_1 = "AI_TURN_1"
    AI_TURN_2 = "AI_TURN_2"
    AI_TURN_3 = "AI_TURN_3"
    AI_TURN_4 = "AI_TURN_4"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    HUMAN_SHARING = "HUMAN_SHARING"
    AI_FOLLOW_UP = "AI_FOLLOW_UP"
    AI_SUMMARY = "AI_SUMMARY"
    POSTCARD_GENERATION = "POSTCARD_GENERATION"
    COMPLETED = "COMPLETED"
    TERMINATED = "TERMINATED"


class RoomStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    TERMINATED = "terminated"


# Define valid transitions
STAGE_TRANSITIONS: dict[ActivityStage, list[ActivityStage]] = {
    ActivityStage.WAITING: [ActivityStage.AI_INTRO, ActivityStage.TERMINATED],
    ActivityStage.AI_INTRO: [ActivityStage.HUMAN_BASE_PAINTING, ActivityStage.TERMINATED],
    ActivityStage.PAINTING_TURN: [
        ActivityStage.PAINTING_TURN,
        ActivityStage.IMAGE_GENERATION,
        ActivityStage.TERMINATED,
    ],
    ActivityStage.ARTWORK_REVEAL: [
        ActivityStage.REFLECTION_QUESTION,
        ActivityStage.TERMINATED,
    ],
    ActivityStage.REFLECTION_QUESTION: [
        ActivityStage.REFLECTION_TURN,
        ActivityStage.TERMINATED,
    ],
    ActivityStage.REFLECTION_TURN: [
        ActivityStage.REFLECTION_TURN,
        ActivityStage.REFLECTION_QUESTION,
        ActivityStage.AI_SUMMARY,
        ActivityStage.TERMINATED,
    ],
    ActivityStage.HUMAN_BASE_PAINTING: [ActivityStage.AI_TURN_1, ActivityStage.TERMINATED],
    ActivityStage.AI_TURN_1: [ActivityStage.AI_TURN_2, ActivityStage.TERMINATED],
    ActivityStage.AI_TURN_2: [ActivityStage.AI_TURN_3, ActivityStage.TERMINATED],
    ActivityStage.AI_TURN_3: [ActivityStage.AI_TURN_4, ActivityStage.TERMINATED],
    ActivityStage.AI_TURN_4: [ActivityStage.IMAGE_GENERATION, ActivityStage.TERMINATED],
    ActivityStage.IMAGE_GENERATION: [ActivityStage.HUMAN_SHARING, ActivityStage.TERMINATED],
    ActivityStage.HUMAN_SHARING: [ActivityStage.AI_FOLLOW_UP, ActivityStage.TERMINATED],
    ActivityStage.AI_FOLLOW_UP: [ActivityStage.AI_SUMMARY, ActivityStage.TERMINATED],
    ActivityStage.AI_SUMMARY: [ActivityStage.POSTCARD_GENERATION, ActivityStage.TERMINATED],
    ActivityStage.POSTCARD_GENERATION: [ActivityStage.COMPLETED, ActivityStage.TERMINATED],
    ActivityStage.COMPLETED: [],
    ActivityStage.TERMINATED: [],
}
