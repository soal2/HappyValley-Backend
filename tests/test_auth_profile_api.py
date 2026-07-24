"""Authentication and profile behavior through the public HTTP API."""

from httpx import AsyncClient


async def test_registration_hashes_password_and_explains_public_immutable_username(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/api/auth/register", json={"username": "星星", "password": "safe-password"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["profile_completed"] is False
    assert "公开游戏昵称" in body["message"]
    assert "不可修改" in body["message"]

    login = await client.post(
        "/api/auth/login", json={"username": "星星", "password": "safe-password"}
    )
    assert login.status_code == 200
    assert "safe-password" not in login.text


async def test_username_is_globally_unique(client: AsyncClient) -> None:
    payload = {"username": "同名", "password": "safe-password"}
    assert (await client.post("/api/auth/register", json=payload)).status_code == 200
    duplicate = await client.post("/api/auth/register", json=payload)
    assert duplicate.status_code == 409


async def test_profile_validates_mbti_and_birth_year(client: AsyncClient) -> None:
    registered = await client.post(
        "/api/auth/register", json={"username": "资料测试", "password": "safe-password"}
    )
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    invalid = await client.post(
        "/api/profile/complete",
        headers=headers,
        json={"mbti": "XXXX", "gender": "female", "birth_year": 3000, "appearance_id": "a"},
    )
    assert invalid.status_code == 422

    valid = await client.post(
        "/api/profile/complete",
        headers=headers,
        json={
            "mbti": "ENFJ",
            "gender": "female",
            "birth_year": 1999,
            "appearance_id": "sprite_02",
        },
    )
    assert valid.status_code == 200
    assert valid.json()["username"] == "资料测试"
