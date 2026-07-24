"""Matching, occupancy, and FIFO queue behavior through HTTP."""

import asyncio

from httpx import AsyncClient

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
    assert snapshot.json()["stage"] == "HUMAN_BASE_PAINTING"


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


async def test_same_account_cannot_queue_or_start_twice(client: AsyncClient) -> None:
    _, headers = await register_user(client, "单占用")
    first = await client.post("/api/activities", headers=headers, json={"mood": "平静"})
    second = await client.post("/api/activities", headers=headers, json={"mood": "平静"})
    assert first.status_code == 201
    assert second.status_code == 409


async def test_concurrent_start_requests_claim_only_one_occupancy(client: AsyncClient) -> None:
    _, headers = await register_user(client, "并发占用")

    async def start() -> int:
        response = await client.post("/api/activities", headers=headers, json={"mood": "并发测试"})
        return response.status_code

    statuses = await asyncio.gather(start(), start())
    assert sorted(statuses) == [201, 409]
