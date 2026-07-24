"""Native WebSocket authentication, snapshot, command, and reconnect contract."""

from fastapi.testclient import TestClient
from httpx import AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from tests.conftest import register_user


async def test_websocket_requires_owner_jwt_and_restores_snapshot(client: AsyncClient) -> None:
    _, headers = await register_user(client, "WS主人")
    started = await client.post("/api/activities", headers=headers, json={"mood": "测试连接"})
    room_id = started.json()["room_id"]
    token = headers["Authorization"].removeprefix("Bearer ")
    _, stranger_headers = await register_user(client, "WS陌生人")
    stranger_token = stranger_headers["Authorization"].removeprefix("Bearer ")

    with TestClient(app) as sync_client:
        try:
            with sync_client.websocket_connect(f"/api/rooms/{room_id}/ws"):
                raise AssertionError("missing token unexpectedly connected")
        except WebSocketDisconnect as error:
            assert error.code == 4401

        try:
            with sync_client.websocket_connect(f"/api/rooms/{room_id}/ws?token={stranger_token}"):
                raise AssertionError("stranger unexpectedly connected")
        except WebSocketDisconnect as error:
            assert error.code == 4403

        with sync_client.websocket_connect(
            f"/api/rooms/{room_id}/ws", subprotocols=["happyvalley.jwt", token]
        ) as socket:
            assert socket.accepted_subprotocol == "happyvalley.jwt"
            first = socket.receive_json()
            assert first["type"] == "snapshot"
            assert first["data"]["room_id"] == room_id
            socket.send_json({"command_id": "ws-heartbeat", "type": "heartbeat", "payload": {}})
            assert socket.receive_json()["type"] == "progress"
            result = socket.receive_json()
            assert result["type"] == "command_result"
            assert result["status"] == "accepted"

        with sync_client.websocket_connect(f"/api/rooms/{room_id}/ws?token={token}") as resumed:
            snapshot = resumed.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["data"]["stage"] == "AI_INTRO"

        with sync_client.websocket_connect(f"/api/rooms/{room_id}/ws?token={token}") as old:
            assert old.receive_json()["type"] == "snapshot"
            with sync_client.websocket_connect(f"/api/rooms/{room_id}/ws?token={token}") as new:
                assert new.receive_json()["type"] == "snapshot"
                try:
                    old.receive_json()
                    raise AssertionError("old connection was not replaced")
                except WebSocketDisconnect as error:
                    assert error.code == 4001
