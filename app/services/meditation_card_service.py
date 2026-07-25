"""Meditation reward card persistence."""

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meditation_card import MeditationCard

ALLOWED_SCENES = frozenset(
    {"beach", "meadow", "waterfall", "aurora-winter-town", "painting-house"}
)


class MeditationCardService:
    @staticmethod
    async def get_user_cards(user_id: str, db: AsyncSession) -> list[MeditationCard]:
        result = await db.execute(
            select(MeditationCard)
            .where(MeditationCard.user_id == user_id)
            .order_by(MeditationCard.created_at.desc())
        )
        return list(result.scalars())

    @staticmethod
    async def create_card(
        user_id: str,
        *,
        scene_key: str,
        card_file: str,
        quote: str,
        nickname: str,
        mbti: str,
        db: AsyncSession,
    ) -> MeditationCard:
        if scene_key not in ALLOWED_SCENES:
            raise HTTPException(status_code=422, detail="未知的冥想场景")
        if "/" in card_file or "\\" in card_file or card_file.startswith("."):
            raise HTTPException(status_code=422, detail="非法的卡片文件名")
        card = MeditationCard(
            user_id=user_id,
            scene_key=scene_key,
            card_file=card_file,
            quote=quote,
            nickname=nickname,
            mbti=mbti,
        )
        db.add(card)
        await db.flush()
        return card
