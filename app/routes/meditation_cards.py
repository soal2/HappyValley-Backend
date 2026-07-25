"""Meditation reward cards: earn by meditating, list for the happy room."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.meditation_card import MeditationCard
from app.models.user import User
from app.schemas.meditation_card import MeditationCardCreate, MeditationCardResponse
from app.services.meditation_card_service import MeditationCardService

router = APIRouter()


def _response(card: MeditationCard) -> MeditationCardResponse:
    return MeditationCardResponse(
        id=card.id,
        scene_key=card.scene_key,
        card_file=card.card_file,
        quote=card.quote,
        nickname=card.nickname,
        mbti=card.mbti,
        created_at=card.created_at.isoformat() if card.created_at else None,
    )


@router.get("/api/meditation-cards", response_model=list[MeditationCardResponse])
async def list_meditation_cards(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[MeditationCardResponse]:
    cards = await MeditationCardService.get_user_cards(user.id, db)
    return [_response(card) for card in cards]


@router.post("/api/meditation-cards", response_model=MeditationCardResponse, status_code=201)
async def create_meditation_card(
    payload: MeditationCardCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MeditationCardResponse:
    card = await MeditationCardService.create_card(
        user.id,
        scene_key=payload.scene_key,
        card_file=payload.card_file,
        quote=payload.quote,
        nickname=payload.nickname,
        mbti=payload.mbti,
        db=db,
    )
    return _response(card)
