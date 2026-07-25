"""Authenticated ElevenLabs TTS proxy for healing-room character speech."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.user import User
from app.services import auth_service
from app.services.tts_service import TtsServiceError, get_tts_service

router = APIRouter()
optional_security = HTTPBearer(auto_error=False)
SpeakerId = Literal["facilitator", "you", "zhu", "xiaoli", "zhang", "li"]


class TtsSynthesizeRequest(BaseModel):
    request_id: str = Field(alias="requestId", min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1000)
    speaker_id: SpeakerId = Field(alias="speakerId")
    language: str = Field(default="zh-CN", min_length=2, max_length=20)

    @field_validator("text")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("语音文本不能为空")
        return value


async def require_tts_access(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(optional_security)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User | None:
    settings = get_settings()
    if credentials is None:
        if settings.MODE.lower() != "production" and settings.TTS_ALLOW_ANONYMOUS_DEMO:
            return None
        raise HTTPException(status_code=401, detail="语音服务需要登录")
    payload = auth_service.verify_token(credentials.credentials)
    user_id = payload.get("sub")
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


@router.post("/api/tts/synthesize")
async def synthesize_speech(
    request: TtsSynthesizeRequest,
    _: Annotated[User | None, Depends(require_tts_access)],
) -> Response:
    try:
        audio = await get_tts_service().synthesize(
            request_id=request.request_id,
            text=request.text,
            speaker_id=request.speaker_id,
            language=request.language,
        )
    except TtsServiceError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code, "message": error.detail},
        ) from error
    headers = {"X-TTS-Cache": "HIT" if audio.cache_hit else "MISS"}
    if audio.duration_ms is not None:
        headers["X-TTS-Duration-Ms"] = str(audio.duration_ms)
    return Response(
        content=audio.content,
        media_type=audio.media_type,
        headers=headers,
    )
