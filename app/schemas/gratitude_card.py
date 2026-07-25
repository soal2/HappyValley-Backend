"""Request/response contracts for gratitude/three-good-things practice cards."""

from pydantic import BaseModel, Field


PRACTICE_TYPES = {"gratitude", "three_good_things"}


class GratitudeCardCreate(BaseModel):
    practice_type: str = Field(min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=500)
    event_location: str = Field(min_length=1, max_length=80)


class GratitudeCardResponse(BaseModel):
    id: str
    practice_type: str
    content: str
    feedback: str
    event_location: str
    created_at: str | None
