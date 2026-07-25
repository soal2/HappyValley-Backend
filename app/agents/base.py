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


class ImageGenerator(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> ImageResult: ...
