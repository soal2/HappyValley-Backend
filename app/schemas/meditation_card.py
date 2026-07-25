"""Request/response contracts for meditation reward cards."""

from pydantic import BaseModel, Field


class MeditationCardCreate(BaseModel):
    scene_key: str = Field(min_length=1, max_length=40)
    card_file: str = Field(min_length=1, max_length=120)
    quote: str = Field(min_length=1, max_length=200)
    nickname: str = Field(min_length=1, max_length=80)
    mbti: str = Field(min_length=1, max_length=8)


class MeditationCardResponse(BaseModel):
    id: str
    scene_key: str
    card_file: str
    quote: str
    nickname: str
    mbti: str
    created_at: str | None
