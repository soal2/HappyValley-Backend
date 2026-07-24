"""Private postcards, revocable public views, and protected media."""

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.container import Container
from app.database import get_db
from app.dependencies import get_current_user
from app.models.postcard import Postcard
from app.models.user import User
from app.schemas.postcard import (
    ArtworkResponse,
    PostcardResponse,
    PublicPostcardResponse,
    ShareResponse,
)
from app.services.postcard_service import PostcardService

router = APIRouter()


def _private_response(postcard: Postcard) -> PostcardResponse:
    return PostcardResponse(
        id=postcard.id,
        artwork=ArtworkResponse(
            image_url=f"/api/postcards/{postcard.id}/media" if postcard.image_path else None,
            text_prompt=postcard.artwork_text,
        ),
        members=list(postcard.member_data or []),
        summary_text=postcard.summary_text,
        encouragement_text=postcard.encouragement_text,
        template_version=postcard.template_version,
        created_at=postcard.created_at.isoformat() if postcard.created_at else None,
    )


def _public_members(members: list[dict[str, object]]) -> list[dict[str, object]]:
    allowed = {"type", "name", "role", "appearance_id", "mbti", "era", "fictional"}
    return [{key: value for key, value in member.items() if key in allowed} for member in members]


@router.get("/api/postcards", response_model=list[PostcardResponse])
async def list_postcards(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[PostcardResponse]:
    postcards = await PostcardService.get_user_postcards(user.id, db)
    return [_private_response(postcard) for postcard in postcards]


@router.get("/api/postcards/{postcard_id}", response_model=PostcardResponse)
async def get_postcard(
    postcard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PostcardResponse:
    return _private_response(await PostcardService.get_postcard(postcard_id, user.id, db))


@router.get("/api/postcards/{postcard_id}/media")
async def get_postcard_media(
    postcard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    postcard = await PostcardService.get_postcard(postcard_id, user.id, db)
    if not postcard.image_path:
        raise HTTPException(status_code=404, detail="本活动只有文字版作品")
    data = await Container.get().media_storage.read(postcard.image_path, user.id)
    return Response(data, media_type="image/png")


@router.post("/api/postcards/{postcard_id}/share", response_model=ShareResponse)
async def share_postcard(
    postcard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ShareResponse:
    token = await PostcardService.create_share_token(postcard_id, user.id, db)
    return ShareResponse(share_token=token, public_path=f"/api/public/postcards/{token}")


@router.delete("/api/postcards/{postcard_id}/share", status_code=204)
async def revoke_share(
    postcard_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    await PostcardService.revoke_share_token(postcard_id, user.id, db)


@router.get("/api/public/postcards/{share_token}", response_model=PublicPostcardResponse)
async def public_postcard(
    share_token: str, response: Response, db: AsyncSession = Depends(get_db)
) -> PublicPostcardResponse:
    postcard = await db.scalar(select(Postcard).where(Postcard.share_token == share_token))
    if postcard is None:
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return PublicPostcardResponse(
        artwork=ArtworkResponse(
            image_url=f"/api/public/postcards/{share_token}/media" if postcard.image_path else None,
            text_prompt=postcard.artwork_text,
        ),
        members=_public_members(list(postcard.member_data or [])),
        encouragement_text=postcard.encouragement_text,
        template_version=postcard.template_version,
    )


@router.get("/api/public/postcards/{share_token}/media")
async def public_postcard_media(share_token: str, db: AsyncSession = Depends(get_db)) -> Response:
    postcard = await db.scalar(select(Postcard).where(Postcard.share_token == share_token))
    if postcard is None or not postcard.image_path:
        raise HTTPException(status_code=404, detail="分享媒体不存在")
    data = await Container.get().media_storage.read(postcard.image_path, postcard.user_id)
    return Response(data, media_type="image/png", headers={"X-Robots-Tag": "noindex, nofollow"})
