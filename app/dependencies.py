"""FastAPI dependencies for authentication and authorization."""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import User
from app.services import auth_service

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract user from JWT token and query DB."""
    token = credentials.credentials
    payload = auth_service.verify_token(token)
    user_id = payload.get("sub")

    result = await db.execute(
        select(User).options(selectinload(User.profile)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    return user


async def require_profile_completed(
    current_user: User = Depends(get_current_user),
) -> User:
    """Check that the user's profile is completed."""
    if current_user.profile is None or not current_user.profile.completed:
        raise HTTPException(status_code=403, detail="请先完善个人资料")
    return current_user


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Check that the current user has admin privileges."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user
