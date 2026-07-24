"""Matching, occupancy, and FIFO queue behavior through HTTP."""

import asyncio

from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session_factory
from app.models.queue import QueueEntry
from tests.conftest import register_user


async def test_incomplete_profile_cannot_start_activity(client: AsyncClient) -> None:
    _, headers = await register_user(client, "未完成", complete_profile=False)
    response = await client.post("/api/activities", headers=headers, json={"mood": "有点紧张"})
    assert response.status_code == 403


async def test_started_room_has_rule_legal_ai_team(client: AsyncClient) -> None:
    _, headers = await register_user(client, "匹配测试")
    response = await client.post("/api/activities", headers=headers, json={"mood": "最近有些孤独"})

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "started"
    snapshot = await client.get(f"/api/rooms/{body['room_id']}", headers=headers)
    team = snapshot.json()["stage_state"]["members"]
    roles = [member["role"] for member in team if member["type"] == "ai_peer"]
    assert roles.count("been-there") == 2
    assert roles.count("different-perspective") == 1
    assert roles.count("quiet-resonator") == 1
    assert len({member["id"] for member in team}) == 5
    assert snapshot.json()["stage"] == "AI_INTRO"


async def test_seventh_user_is_fifo_queued_then_gets_sixty_second_reservation(
    client: AsyncClient,
) -> None:
    users: list[tuple[dict[str, str], str]] = []
    for index in range(7):
        _, headers = await register_user(client, f"排队{index}")
        response = await client.post(
            "/api/activities", headers=headers, json={"mood": f"心情{index}"}
        )
        if index < 6:
            assert response.status_code == 201
            users.append((headers, response.json()["room_id"]))
        else:
            assert response.status_code == 202
            assert response.json()["queue_position"] == 1
            queued_headers = headers

    await client.post(
        f"/api/rooms/{users[0][1]}/commands",
        headers=users[0][0],
        json={"command_id": "exit-1", "type": "exit", "payload": {}},
    )
    position = await client.get("/api/queue/me", headers=queued_headers)
    assert position.status_code == 200
    assert position.json()["status"] == "reserved"
    assert position.json()["reservation_expires_at"] is not None

    confirmed = await client.post("/api/queue/confirm", headers=queued_headers)
    assert confirmed.status_code == 201
    assert confirmed.json()["status"] == "started"


async def test_same_account_resumes_existing_room_instead_of_starting_twice(
    client: AsyncClient,
) -> None:
    _, headers = await register_user(client, "单占用")
    first = await client.post("/api/activities", headers=headers, json={"mood": "平静"})
    second = await client.post("/api/activities", headers=headers, json={"mood": "刷新后的心情"})
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["room_id"] == first.json()["room_id"]
    recovered = await client.get("/api/rooms/active", headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["room_id"] == first.json()["room_id"]


async def test_concurrent_start_requests_claim_only_one_occupancy(client: AsyncClient) -> None:
    _, headers = await register_user(client, "并发占用")

    async def start() -> int:
        response = await client.post("/api/activities", headers=headers, json={"mood": "并发测试"})
        return response.status_code

    statuses = await asyncio.gather(start(), start())
    assert sorted(statuses) == [201, 409]


async def test_queued_user_can_cancel_and_original_mood_is_persisted(
    client: AsyncClient,
) -> None:
    for index in range(6):
        _, headers = await register_user(client, f"占位{index}")
        started = await client.post(
            "/api/activities", headers=headers, json={"mood": f"占位心情{index}"}
        )
        assert started.status_code == 201

    user_id, queued_headers = await register_user(client, "取消排队")
    mood = "这是必须原样保留的排队心情"
    queued = await client.post("/api/activities", headers=queued_headers, json={"mood": mood})
    assert queued.status_code == 202
    async with async_session_factory() as session:
        entry = await session.scalar(select(QueueEntry).where(QueueEntry.user_id == user_id))
        assert entry is not None
        assert entry.mood == mood

    cancelled = await client.delete("/api/queue/me", headers=queued_headers)
    assert cancelled.status_code == 204
    status = await client.get("/api/queue/me", headers=queued_headers)
    assert status.json()["status"] == "none"
