"""Abstract base classes for AI agent runtime and image generation."""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class RankedMatch(BaseModel):
    character_id: str
    rank: int
    reason: str


class SummaryResult(BaseModel):
    summary_text: str
    encouragement_text: str
    generation: "GenerationMeta | None" = None


class GenerationMeta(BaseModel):
    source: str
    model: str
    request_id: str


class TextGenerationResult(BaseModel):
    text: str = Field(min_length=1)
    generation: GenerationMeta


class ReflectionAnswerResult(BaseModel):
    text: str = Field(min_length=1)
    positive_feedback_type: str
    referenced_message_ids: list[str] = Field(default_factory=list)
    generation: GenerationMeta


class ImageResult(BaseModel):
    data: bytes | None = None
    status: str  # "completed" | "failed"
    error: str | None = None
    generation: GenerationMeta | None = None


# --- Dynamic teammate generation models (5-dimension persona framework) ---


class BigFive(BaseModel):
    openness: float
    conscientiousness: float
    extraversion: float
    agreeableness: float
    neuroticism: float


class CognitiveStyle(BaseModel):
    decision_making: str
    risk_taking: str


class Motivation(BaseModel):
    intrinsic: list[str]
    extrinsic: list[str]


class ExpressionStyle(BaseModel):
    sentence_length: str
    humor: str
    metaphor_frequency: str


class HistoricalContext(BaseModel):
    era: str
    profession: str
    cultural_background: str
    key_events: list[str]


class FiveDimPersona(BaseModel):
    big_five: BigFive
    cognitive_style: CognitiveStyle
    motivation: Motivation
    expression_style: ExpressionStyle
    historical_context: HistoricalContext


class ConceptualSkeleton(BaseModel):
    name: str
    era: str
    mbti: str
    role: str  # been-there | different-perspective | quiet-resonator
    one_line_concept: str


class TeamSkeletonResult(BaseModel):
    real_character_ids: list[str]
    conceptuals: list[ConceptualSkeleton]


class ConceptualPersonaResult(BaseModel):
    five_dim: FiveDimPersona
    system_prompt_template: str = Field(min_length=1)
    speak_style: str = Field(min_length=1)
    facts_boundaries: str = Field(min_length=1)
    safety_fallback: str = Field(min_length=1)
    topics: list[str]
    generation: GenerationMeta


class AgentRuntime(ABC):
    @abstractmethod
    async def rank_candidates(
        self, user_mood: str, user_age: int, candidates: list[dict[str, object]]
    ) -> list[RankedMatch]: ...

    @abstractmethod
    async def generate_opening(
        self,
        members: list[dict[str, object]],
        activity_rules: list[str],
        first_speaker: dict[str, object],
        max_duration_seconds: int = 20,
    ) -> TextGenerationResult: ...

    @abstractmethod
    async def generate_peer_turn(
        self,
        character: dict[str, object],
        previous_contributions: list[str],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult: ...

    @abstractmethod
    async def generate_facilitator_response(
        self, context: str, user_sharing: str
    ) -> TextGenerationResult: ...

    @abstractmethod
    async def generate_reflection_question(
        self,
        question_index: int,
        intent: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> TextGenerationResult: ...

    @abstractmethod
    async def generate_reflection_answer(
        self,
        character: dict[str, object],
        question: str,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 10,
    ) -> ReflectionAnswerResult: ...

    @abstractmethod
    async def generate_summary(
        self,
        context: str,
        transcript: list[dict[str, object]],
        max_duration_seconds: int = 20,
    ) -> SummaryResult: ...

    @abstractmethod
    async def generate_postcard_text(
        self, context: str, user_sharing: str
    ) -> TextGenerationResult: ...

    @abstractmethod
    async def select_team_skeletons(
        self,
        user_mood: str,
        user_age: int,
        user_profile: dict[str, object],
        whitelist_candidates: list[dict[str, object]],
    ) -> TeamSkeletonResult: ...

    @abstractmethod
    async def generate_conceptual_persona(
        self,
        skeleton: dict[str, object],
        user_mood: str,
        user_profile: dict[str, object],
    ) -> ConceptualPersonaResult: ...


class ImageGenerator(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> ImageResult: ...
