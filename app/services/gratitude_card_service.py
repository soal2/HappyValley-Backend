"""Gratitude/three-good-things practice card persistence + AI feedback."""

import structlog
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.base import AgentRuntime
from app.models.gratitude_card import GratitudeCard
from app.schemas.gratitude_card import PRACTICE_TYPES

logger = structlog.get_logger()

# AI 失败时的兜底反馈（按 practice_type 区分）
FALLBACK_FEEDBACK = {
    "gratitude": "愿意把感谢写下来，本身就是一份温柔的力量。",
    "three_good_things": "能留心这些小小的亮光，心也会被慢慢照亮。",
}


class GratitudeCardService:
    @staticmethod
    async def get_user_cards(user_id: str, db: AsyncSession) -> list[GratitudeCard]:
        result = await db.execute(
            select(GratitudeCard)
            .where(GratitudeCard.user_id == user_id)
            .order_by(GratitudeCard.created_at.desc())
        )
        return list(result.scalars())

    @staticmethod
    async def create_card(
        user_id: str,
        *,
        practice_type: str,
        content: str,
        event_location: str,
        runtime: AgentRuntime,
        db: AsyncSession,
    ) -> GratitudeCard:
        if practice_type not in PRACTICE_TYPES:
            raise HTTPException(status_code=422, detail="未知的练习类型")
        # 调用 AI 生成反馈，失败时用兜底文案，不阻断出卡
        feedback = FALLBACK_FEEDBACK.get(practice_type, "谢谢你愿意写下这些。")
        try:
            result = await runtime.generate_practice_feedback(practice_type, content)
            feedback = result.text
        except Exception as e:
            logger.warning(
                "gratitude_card.feedback_failed",
                practice_type=practice_type,
                error_type=type(e).__name__,
                error=str(e),
            )
        card = GratitudeCard(
            user_id=user_id,
            practice_type=practice_type,
            content=content,
            feedback=feedback,
            event_location=event_location,
        )
        db.add(card)
        await db.flush()
        return card
