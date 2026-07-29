"""Admin routes: platform statistics for admin users."""

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_admin
from app.models.feedback import Feedback
from app.models.user import User, UserProfile
from app.schemas.feedback import AdminStatsResponse

router = APIRouter()


@router.get("/api/admin/stats", response_model=AdminStatsResponse)
async def get_admin_stats(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminStatsResponse:
    """Return platform statistics (admin only)."""
    total_result = await db.execute(select(func.count()).select_from(User))
    total_users = total_result.scalar() or 0

    completed_result = await db.execute(
        select(func.count()).select_from(UserProfile).where(UserProfile.completed.is_(True))
    )
    completed_profiles = completed_result.scalar() or 0

    feedback_result = await db.execute(select(func.count()).select_from(Feedback))
    total_feedback = feedback_result.scalar() or 0

    return AdminStatsResponse(
        total_users=total_users,
        completed_profiles=completed_profiles,
        total_feedback=total_feedback,
    )
