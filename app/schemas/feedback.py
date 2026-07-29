"""Pydantic schemas for feedback and replies."""

from datetime import datetime

from pydantic import BaseModel, Field


class FeedbackCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    parent_id: str | None = None


class FeedbackReplyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class FeedbackItem(BaseModel):
    id: str
    user_id: str
    username: str
    content: str
    created_at: datetime
    like_count: int
    liked_by_me: bool
    replies: list["FeedbackItem"] = []


class FeedbackListResponse(BaseModel):
    items: list[FeedbackItem]


class AdminStatsResponse(BaseModel):
    total_users: int
    completed_profiles: int
    total_feedback: int
