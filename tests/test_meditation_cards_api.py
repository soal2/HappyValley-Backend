"""Meditation reward card API: create, list, per-user isolation, validation."""

from httpx import AsyncClient

from tests.conftest import register_user


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scene_key": "beach",
        "card_file": "card_01.jpg",
        "quote": "听潮起潮落，心也跟着柔软下来。",
        "nickname": "小谷",
        "mbti": "INFP",
    }
    base.update(overrides)
    return base


async def test_create_and_list_meditation_cards(client: AsyncClient) -> None:
    _, headers = await register_user(client)

    created = await client.post("/api/meditation-cards", headers=headers, json=payload())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["scene_key"] == "beach"
    assert body["created_at"]

    listed = await client.get("/api/meditation-cards", headers=headers)
    assert listed.status_code == 200
    cards = listed.json()
    assert len(cards) == 1
    assert cards[0]["id"] == body["id"]
    assert cards[0]["quote"] == payload()["quote"]


async def test_meditation_cards_are_private_per_user(client: AsyncClient) -> None:
    _, headers_a = await register_user(client, username="用户甲")
    _, headers_b = await register_user(client, username="用户乙")

    assert (
        await client.post("/api/meditation-cards", headers=headers_a, json=payload())
    ).status_code == 201

    listed = await client.get("/api/meditation-cards", headers=headers_b)
    assert listed.status_code == 200
    assert listed.json() == []


async def test_meditation_card_rejects_unknown_scene(client: AsyncClient) -> None:
    _, headers = await register_user(client)
    response = await client.post(
        "/api/meditation-cards",
        headers=headers,
        json=payload(scene_key="not-a-scene"),
    )
    assert response.status_code == 422


async def test_meditation_card_rejects_unsafe_card_file(client: AsyncClient) -> None:
    _, headers = await register_user(client)
    response = await client.post(
        "/api/meditation-cards",
        headers=headers,
        json=payload(card_file="../secret.png"),
    )
    assert response.status_code == 422


async def test_meditation_cards_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/meditation-cards")).status_code == 401
    assert (
        await client.post("/api/meditation-cards", json=payload())
    ).status_code == 401
