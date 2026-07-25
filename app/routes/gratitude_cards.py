"""Gratitude/three-good-things practice cards API."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.database import get_db
from app.dependencies import get_current_user
from app.models.gratitude_card import GratitudeCard
from app.models.user import User
from app.schemas.gratitude_card import (
    GratitudeCardCreate,
    GratitudeCardResponse,
)
from app.services.gratitude_card_service import GratitudeCardService

router = APIRouter()


def _response(card: GratitudeCard) -> GratitudeCardResponse:
    return GratitudeCardResponse(
        id=card.id,
        practice_type=card.practice_type,
        content=card.content,
        feedback=card.feedback,
        event_location=card.event_location,
        created_at=card.created_at.isoformat() if card.created_at else None,
    )


@router.get(
    "/api/gratitude-cards", response_model=list[GratitudeCardResponse]
)
async def list_gratitude_cards(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[GratitudeCardResponse]:
    cards = await GratitudeCardService.get_user_cards(user.id, db)
    return [_response(card) for card in cards]


@router.post(
    "/api/gratitude-cards",
    response_model=GratitudeCardResponse,
    status_code=201,
)
async def create_gratitude_card(
    payload: GratitudeCardCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> GratitudeCardResponse:
    runtime = Container.get().agent_runtime
    card = await GratitudeCardService.create_card(
        user.id,
        practice_type=payload.practice_type,
        content=payload.content,
        event_location=payload.event_location,
        runtime=runtime,
        db=db,
    )
    await db.commit()
    return _response(card)
