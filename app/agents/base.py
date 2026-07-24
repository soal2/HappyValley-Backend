"""Abstract base classes for AI agent runtime and image generation."""

from abc import ABC, abstractmethod

from pydantic import BaseModel


class RankedMatch(BaseModel):
    character_id: str
    rank: int
    reason: str


class SummaryResult(BaseModel):
    summary_text: str
    encouragement_text: str


class ImageResult(BaseModel):
    data: bytes | None = None
    status: str  # "completed" | "failed"
    error: str | None = None


class AgentRuntime(ABC):
    @abstractmethod
    async def rank_candidates(
        self, user_mood: str, user_age: int, candidates: list[dict[str, object]]
    ) -> list[RankedMatch]: ...

    @abstractmethod
    async def generate_ai_turns(
        self,
        context: str,
        characters: list[dict[str, object]],
        previous_contributions: list[str],
    ) -> list[str]: ...

    @abstractmethod
    async def generate_follow_up(self, context: str, user_sharing: str) -> str: ...

    @abstractmethod
    async def generate_summary(
        self, context: str, transcript: list[dict[str, object]]
    ) -> SummaryResult: ...

    @abstractmethod
    async def generate_postcard_text(self, context: str, user_sharing: str) -> str: ...


class ImageGenerator(ABC):
    @abstractmethod
    async def generate(self, prompt: str) -> ImageResult: ...
