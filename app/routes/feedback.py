"""Feedback routes: list, create, reply, like/unlike."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import require_profile_completed
from app.models.feedback import Feedback, FeedbackLike
from app.models.user import User
from app.schemas.feedback import (
    FeedbackCreateRequest,
    FeedbackItem,
    FeedbackListResponse,
)

router = APIRouter()


def _build_item(
    fb: Feedback,
    user: User,
    like_count: int,
    liked_by_me: bool,
    replies: list[FeedbackItem],
) -> FeedbackItem:
    return FeedbackItem(
        id=fb.id,
        user_id=fb.user_id,
        username=user.username,
        content=fb.content,
        created_at=fb.created_at,
        like_count=like_count,
        liked_by_me=liked_by_me,
        replies=replies,
    )


@router.get("/api/feedback", response_model=FeedbackListResponse)
async def list_feedback(
    current_user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> FeedbackListResponse:
    """List all top-level feedback with replies and like info."""
    # Fetch top-level feedback (parent_id is None), ordered newest first.
    top_result = await db.execute(
        select(Feedback)
        .options(selectinload(Feedback.user))
        .where(Feedback.parent_id.is_(None))
        .order_by(Feedback.created_at.desc())
    )
    top_items = top_result.scalars().all()

    # Fetch all replies for these top-level items.
    top_ids = [t.id for t in top_items]
    replies: list[Feedback] = []
    if top_ids:
        reply_result = await db.execute(
            select(Feedback)
            .options(selectinload(Feedback.user))
            .where(Feedback.parent_id.in_(top_ids))
            .order_by(Feedback.created_at.asc())
        )
        replies = list(reply_result.scalars().all())

    # Fetch all likes for top-level + replies.
    all_ids = top_ids + [r.id for r in replies]
    like_counts: dict[str, int] = {}
    liked_by_me_ids: set[str] = set()
    if all_ids:
        count_result = await db.execute(
            select(FeedbackLike.feedback_id, func.count().label("cnt"))
            .where(FeedbackLike.feedback_id.in_(all_ids))
            .group_by(FeedbackLike.feedback_id)
        )
        like_counts = {row.feedback_id: row.cnt for row in count_result}

        mine_result = await db.execute(
            select(FeedbackLike.feedback_id).where(
                FeedbackLike.feedback_id.in_(all_ids),
                FeedbackLike.user_id == current_user.id,
            )
        )
        liked_by_me_ids = {row[0] for row in mine_result}

    # Group replies by parent.
    replies_by_parent: dict[str, list[Feedback]] = {}
    for r in replies:
        replies_by_parent.setdefault(r.parent_id, []).append(r)

    items: list[FeedbackItem] = []
    for fb in top_items:
        reply_items = [
            _build_item(
                r,
                r.user,
                like_counts.get(r.id, 0),
                r.id in liked_by_me_ids,
                [],
            )
            for r in replies_by_parent.get(fb.id, [])
        ]
        items.append(
            _build_item(
                fb,
                fb.user,
                like_counts.get(fb.id, 0),
                fb.id in liked_by_me_ids,
                reply_items,
            )
        )

    return FeedbackListResponse(items=items)


@router.post("/api/feedback", response_model=FeedbackItem, status_code=201)
async def create_feedback(
    req: FeedbackCreateRequest,
    current_user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> FeedbackItem:
    """Create a new feedback message (or reply if parent_id is given)."""
    if req.parent_id:
        result = await db.execute(
            select(Feedback).where(Feedback.id == req.parent_id)
        )
        parent = result.scalar_one_or_none()
        if parent is None:
            raise HTTPException(status_code=404, detail="父留言不存在")
        # Only allow one level of nesting.
        if parent.parent_id is not None:
            raise HTTPException(status_code=400, detail="不支持多级回复")

    fb = Feedback(
        user_id=current_user.id,
        parent_id=req.parent_id,
        content=req.content.strip(),
    )
    db.add(fb)
    await db.flush()

    return _build_item(fb, current_user, 0, False, [])


@router.post("/api/feedback/{feedback_id}/like", status_code=201)
async def like_feedback(
    feedback_id: str,
    current_user: User = Depends(require_profile_completed),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Toggle like on a feedback message. Returns current like count and state."""
    result = await db.execute(
        select(Feedback).where(Feedback.id == feedback_id)
    )
    fb = result.scalar_one_or_none()
    if fb is None:
        raise HTTPException(status_code=404, detail="留言不存在")

    existing = await db.execute(
        select(FeedbackLike).where(
            FeedbackLike.feedback_id == feedback_id,
            FeedbackLike.user_id == current_user.id,
        )
    )
    existing_like = existing.scalar_one_or_none()

    if existing_like:
        await db.delete(existing_like)
        liked = False
    else:
        db.add(FeedbackLike(feedback_id=feedback_id, user_id=current_user.id))
        liked = True

    await db.flush()

    count_result = await db.execute(
        select(func.count()).select_from(FeedbackLike).where(
            FeedbackLike.feedback_id == feedback_id
        )
    )
    count = count_result.scalar() or 0

    return {"liked": liked, "like_count": count}
