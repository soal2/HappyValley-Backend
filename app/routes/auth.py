"""Auth and profile routes."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User, UserProfile
from app.schemas.auth import (
    LoginRequest,
    ProfileCompleteRequest,
    ProfileResponse,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services import auth_service

router = APIRouter()


@router.post("/api/auth/register", response_model=TokenResponse)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Register a new user."""
    user = await auth_service.register(req.username, req.password, db)
    token = auth_service.create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        profile_completed=False,
        message="用户名注册后不可修改，将作为您的公开游戏昵称",
    )


@router.post("/api/auth/login", response_model=TokenResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    """Login and obtain JWT."""
    user = await auth_service.login(req.username, req.password, db)
    # Load profile to check completed status
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == user.id))
    profile = result.scalar_one_or_none()
    profile_completed = profile.completed if profile else False

    token = auth_service.create_token(user.id, user.username)
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        username=user.username,
        profile_completed=profile_completed,
    )


@router.get("/api/auth/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """Return current user info."""
    profile_completed = current_user.profile.completed if current_user.profile else False
    return UserResponse(
        user_id=current_user.id,
        username=current_user.username,
        profile_completed=profile_completed,
    )


@router.post("/api/profile/complete", response_model=ProfileResponse)
async def complete_profile(
    req: ProfileCompleteRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """Save profile fields and mark as completed."""
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    profile = result.scalar_one_or_none()
    if profile is None:
        profile = UserProfile(user_id=current_user.id)
        db.add(profile)

    profile.mbti = req.mbti
    profile.gender = req.gender
    profile.birth_year = req.birth_year
    profile.appearance_id = req.appearance_id
    profile.completed = True
    await db.flush()

    return ProfileResponse(
        user_id=current_user.id,
        username=current_user.username,
        mbti=profile.mbti,
        gender=profile.gender,
        birth_year=profile.birth_year,
        appearance_id=profile.appearance_id,
        completed=profile.completed,
    )


@router.get("/api/profile", response_model=ProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProfileResponse:
    """Return profile data."""
    result = await db.execute(select(UserProfile).where(UserProfile.user_id == current_user.id))
    profile = result.scalar_one_or_none()

    return ProfileResponse(
        user_id=current_user.id,
        username=current_user.username,
        mbti=profile.mbti if profile else None,
        gender=profile.gender if profile else None,
        birth_year=profile.birth_year if profile else None,
        appearance_id=profile.appearance_id if profile else None,
        completed=profile.completed if profile else False,
    )
