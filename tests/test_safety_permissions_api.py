"""Crisis, ownership, sharing, and cleanup boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session_factory
from app.models.room import ActivityRoom
from tests.conftest import register_user


async def _start(client: AsyncClient, username: str) -> tuple[str, dict[str, str]]:
    _, headers = await register_user(client, username)
    response = await client.post("/api/activities", headers=headers, json={"mood": "今天想画画"})
    room_id = response.json()["room_id"]
    return room_id, headers


async def _wait_for_painting_turn(
    client: AsyncClient, room_id: str, headers: dict[str, str]
) -> dict[str, object]:
    for _ in range(100):
        snapshot = (await client.get(f"/api/rooms/{room_id}", headers=headers)).json()
        if snapshot.get("stage") == "PAINTING_TURN":
            return snapshot
        await asyncio.sleep(0.05)
    raise AssertionError(f"pre-roll did not reach PAINTING_TURN, last stage={snapshot.get('stage')!r}")


async def _complete(client: AsyncClient, room_id: str, headers: dict[str, str]) -> str:
    initial = await _wait_for_painting_turn(client, room_id, headers)
    painted = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "painting",
            "type": "submit_painting_turn",
            "payload": {
                "turn_index": initial["stage_state"]["painting_turn_index"],
                "text": "海边的一棵树",
            },
        },
    )
    current = painted.json()["snapshot"]
    revealed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "reveal",
            "type": "complete_artwork_reveal",
            "payload": {"expected_revision": current["revision"]},
        },
    )
    current = revealed.json()["snapshot"]
    for round_index in range(4):
        answered = await client.post(
            f"/api/rooms/{room_id}/commands",
            headers=headers,
            json={
                "command_id": f"reflection-{round_index}",
                "type": "submit_reflection",
                "payload": {
                    "round_index": round_index,
                    "text": f"第 {round_index + 1} 轮里，海风让我感到放松",
                },
            },
        )
        current = answered.json()["snapshot"]
    return current["stage_state"]["postcard_id"]


async def test_crisis_text_is_blocked_not_persisted_and_can_resume(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "安全用户")
    initial = await _wait_for_painting_turn(client, room_id, headers)
    crisis_text = "我现在想自杀然后结束生命"
    blocked = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "crisis-1",
            "type": "submit_painting_turn",
            "payload": {
                "turn_index": initial["stage_state"]["painting_turn_index"],
                "text": crisis_text,
            },
        },
    )
    assert blocked.status_code == 200
    snapshot = blocked.json()["snapshot"]
    assert snapshot["stage"] == "PAINTING_TURN"
    assert snapshot["stage_state"]["crisis"]["crisis_triggered"] is True
    assert crisis_text not in str(snapshot)
    assert all(message["text"] != crisis_text for message in snapshot["messages"])

    resumed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "crisis-resume",
            "type": "crisis_response",
            "payload": {"can_continue": True},
        },
    )
    assert "crisis" not in resumed.json()["snapshot"]["stage_state"]
    safe = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "base-safe",
            "type": "submit_painting_turn",
            "payload": {
                "turn_index": snapshot["stage_state"]["painting_turn_index"],
                "text": "窗边的一盆绿植",
            },
        },
    )
    assert safe.json()["snapshot"]["stage"] == "ARTWORK_REVEAL"


async def test_crisis_wait_timeout_terminates_without_postcard(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "危机等待")
    snapshot = await _wait_for_painting_turn(client, room_id, headers)
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "crisis",
            "type": "submit_painting_turn",
            "payload": {
                "turn_index": snapshot["stage_state"]["painting_turn_index"],
                "text": "我想自伤",
            },
        },
    )
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "pause",
            "type": "crisis_response",
            "payload": {"can_continue": False},
        },
    )
    async with async_session_factory() as session:
        room = await session.scalar(select(ActivityRoom).where(ActivityRoom.id == room_id))
        assert room is not None
        room.disconnected_at = (datetime.now(UTC) - timedelta(minutes=6)).replace(tzinfo=None)
        await session.commit()

    timeout = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={"command_id": "late", "type": "heartbeat", "payload": {}},
    )
    assert timeout.status_code == 200
    snapshot = await client.get(f"/api/rooms/{room_id}", headers=headers)
    assert snapshot.json()["stage"] == "TERMINATED"
    assert (await client.get("/api/postcards", headers=headers)).json() == []


async def test_private_resources_and_revocable_public_view(client: AsyncClient) -> None:
    room_id, owner_headers = await _start(client, "明信片主人")
    postcard_id = await _complete(client, room_id, owner_headers)
    owner_id = (await client.get("/api/auth/me", headers=owner_headers)).json()["user_id"]
    _, stranger_headers = await register_user(client, "陌生人")

    assert (await client.get(f"/api/rooms/{room_id}", headers=stranger_headers)).status_code == 403
    assert (
        await client.get(f"/api/postcards/{postcard_id}", headers=stranger_headers)
    ).status_code == 403
    assert (
        await client.get(f"/api/postcards/{postcard_id}/media", headers=stranger_headers)
    ).status_code == 403

    shared = await client.post(f"/api/postcards/{postcard_id}/share", headers=owner_headers)
    public_path = shared.json()["public_path"]
    public = await client.get(public_path)
    assert public.status_code == 200
    assert public.headers["x-robots-tag"] == "noindex, nofollow"
    assert "user_id" not in public.text
    assert "username" not in public.text
    assert owner_id not in public.text

    revoked = await client.delete(f"/api/postcards/{postcard_id}/share", headers=owner_headers)
    assert revoked.status_code == 204
    assert (await client.get(public_path)).status_code == 404
