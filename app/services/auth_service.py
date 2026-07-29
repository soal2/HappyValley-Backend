"""Authentication service: registration, login, JWT token management."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from jose import JWTError, jwt
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User, UserProfile

password_hash = PasswordHash.recommended()


def _hash_password(password: str) -> str:
    return password_hash.hash(password)


def _verify_password(plain: str, hashed: str) -> bool:
    return password_hash.verify(plain, hashed)


async def register(username: str, password: str, db: AsyncSession) -> User:
    """Create a new user with hashed password and empty profile."""
    # Check if username already taken
    result = await db.execute(select(User).where(User.username == username))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    settings = get_settings()
    admin_names = {
        name.strip().lower()
        for name in settings.ADMIN_USERNAMES.split(",")
        if name.strip()
    }

    user = User(
        username=username,
        password_hash=_hash_password(password),
        is_admin=username.lower() in admin_names,
    )
    db.add(user)
    await db.flush()

    # Create empty profile
    profile = UserProfile(user_id=user.id, completed=False)
    db.add(profile)
    await db.flush()

    return user


async def login(username: str, password: str, db: AsyncSession) -> User:
    """Verify credentials and return user or raise."""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not _verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return user


def create_token(user_id: str, username: str) -> str:
    """Create a JWT access token."""
    settings = get_settings()
    expire = datetime.now(UTC) + timedelta(hours=settings.JWT_EXPIRATION_HOURS)
    payload = {
        "sub": user_id,
        "username": username,
        "exp": expire,
    }
    return cast(str, jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM))


def verify_token(token: str) -> dict[str, Any]:
    """Decode JWT and return payload or raise."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        if payload.get("sub") is None:
            raise HTTPException(status_code=401, detail="无效的认证令牌")
        return cast(dict[str, Any], payload)
    except JWTError:
        raise HTTPException(status_code=401, detail="无效的认证令牌") from None
