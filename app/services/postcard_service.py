"""Private postcard retrieval and revocable sharing."""

import secrets
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.postcard import Postcard


class PostcardService:
    @staticmethod
    async def get_user_postcards(user_id: str, db: AsyncSession) -> list[Postcard]:
        result = await db.execute(
            select(Postcard).where(Postcard.user_id == user_id).order_by(Postcard.created_at.desc())
        )
        return list(result.scalars())

    @staticmethod
    async def get_postcard(postcard_id: str, user_id: str, db: AsyncSession) -> Postcard:
        postcard = await db.scalar(select(Postcard).where(Postcard.id == postcard_id))
        if postcard is None:
            raise HTTPException(status_code=404, detail="明信片不存在")
        if postcard.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问此明信片")
        return postcard

    @staticmethod
    async def create_share_token(postcard_id: str, user_id: str, db: AsyncSession) -> str:
        postcard = await PostcardService.get_postcard(postcard_id, user_id, db)
        postcard.share_token = secrets.token_urlsafe(32)
        postcard.shared_at = datetime.now(UTC)
        await db.flush()
        return postcard.share_token

    @staticmethod
    async def revoke_share_token(postcard_id: str, user_id: str, db: AsyncSession) -> None:
        postcard = await PostcardService.get_postcard(postcard_id, user_id, db)
        postcard.share_token = None
        postcard.shared_at = None
        await db.flush()
