"""Auth and profile Pydantic schemas."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

VALID_MBTI = {
    f"{e}{s}{t}{j}" for e in ("E", "I") for s in ("S", "N") for t in ("T", "F") for j in ("J", "P")
}


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=20, pattern=r"^\S(?:.*\S)?$")
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    username: str
    profile_completed: bool
    message: str | None = None


class ProfileCompleteRequest(BaseModel):
    mbti: str
    gender: str = Field(min_length=1, max_length=20)
    birth_year: int
    appearance_id: str = Field(min_length=1, max_length=100)

    @field_validator("mbti")
    @classmethod
    def validate_mbti(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in VALID_MBTI:
            raise ValueError("MBTI 必须是有效的 16 型之一")
        return normalized

    @field_validator("birth_year")
    @classmethod
    def validate_birth_year(cls, value: int) -> int:
        current_year = datetime.now(UTC).year
        if value < 1900 or value > current_year:
            raise ValueError("出生年份无效")
        return value


class ProfileResponse(BaseModel):
    user_id: str
    username: str
    mbti: str | None = None
    gender: str | None = None
    birth_year: int | None = None
    appearance_id: str | None = None
    completed: bool


class UserResponse(BaseModel):
    user_id: str
    username: str
    profile_completed: bool
