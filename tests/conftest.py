"""Shared integration-test fixtures."""

import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

TEST_DB_PATH = Path(__file__).parent / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_PATH}"
os.environ["SECRET_KEY"] = "test-only-secret"
os.environ["MODE"] = "test"
os.environ["MEDIA_STORAGE_PATH"] = str(Path(__file__).parent / "media")

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    shutil.rmtree(Path(__file__).parent / "media", ignore_errors=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as test_client:
        yield test_client


async def register_user(
    client: AsyncClient, username: str = "小谷", complete_profile: bool = True
) -> tuple[str, dict[str, str]]:
    response = await client.post(
        "/api/auth/register", json={"username": username, "password": "safe-password"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    if complete_profile:
        profile = await client.post(
            "/api/profile/complete",
            headers=headers,
            json={
                "mbti": "INFP",
                "gender": "unspecified",
                "birth_year": 2000,
                "appearance_id": "sprite_01",
            },
        )
        assert profile.status_code == 200, profile.text
    return body["user_id"], headers
