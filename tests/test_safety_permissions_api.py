"""Crisis, ownership, sharing, and cleanup boundaries."""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

from app.database import async_session_factory
from app.models.room import ActivityRoom
from tests.conftest import register_user


async def _start(client: AsyncClient, username: str) -> tuple[str, dict[str, str]]:
    _, headers = await register_user(client, username)
    response = await client.post("/api/activities", headers=headers, json={"mood": "今天想画画"})
    return response.json()["room_id"], headers


async def _complete(client: AsyncClient, room_id: str, headers: dict[str, str]) -> str:
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "base",
            "type": "submit_base",
            "payload": {"text": "海边的一棵树"},
        },
    )
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "share",
            "type": "submit_sharing",
            "payload": {"text": "海风让我放松"},
        },
    )
    completed = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={"command_id": "skip", "type": "skip_follow_up", "payload": {}},
    )
    return completed.json()["snapshot"]["stage_state"]["postcard_id"]


async def test_crisis_text_is_blocked_not_persisted_and_can_resume(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "安全用户")
    crisis_text = "我现在想自杀然后结束生命"
    blocked = await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "crisis-1",
            "type": "submit_base",
            "payload": {"text": crisis_text},
        },
    )
    assert blocked.status_code == 200
    snapshot = blocked.json()["snapshot"]
    assert snapshot["stage"] == "HUMAN_BASE_PAINTING"
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
            "type": "submit_base",
            "payload": {"text": "窗边的一盆绿植"},
        },
    )
    assert safe.json()["snapshot"]["stage"] == "HUMAN_SHARING"


async def test_crisis_wait_timeout_terminates_without_postcard(client: AsyncClient) -> None:
    room_id, headers = await _start(client, "危机等待")
    await client.post(
        f"/api/rooms/{room_id}/commands",
        headers=headers,
        json={
            "command_id": "crisis",
            "type": "submit_base",
            "payload": {"text": "我想自伤"},
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
