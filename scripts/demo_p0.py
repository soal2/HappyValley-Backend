"""Run one complete demo-mode P0 activity against a running API."""

import os
import time

import httpx


def main() -> None:
    base_url = os.getenv("HAPPYVALLEY_BASE_URL", "http://127.0.0.1:8000")
    username = f"demo_{int(time.time())}"
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as client:
        registered = client.post(
            "/api/auth/register", json={"username": username, "password": "demo-password"}
        )
        registered.raise_for_status()
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        profile = client.post(
            "/api/profile/complete",
            headers=headers,
            json={
                "mbti": "INFP",
                "gender": "unspecified",
                "birth_year": 2000,
                "appearance_id": "sprite_01",
            },
        )
        profile.raise_for_status()
        started = client.post("/api/activities", headers=headers, json={"mood": "想画一幅温暖的画"})
        started.raise_for_status()
        room_id = started.json()["room_id"]

        commands = [
            {
                "command_id": "demo-base",
                "type": "submit_base",
                "payload": {"text": "山谷中央一棵正在发芽的小树"},
            },
            {
                "command_id": "demo-share",
                "type": "submit_sharing",
                "payload": {"text": "这棵树让我想到成长可以很慢"},
            },
            {"command_id": "demo-skip", "type": "skip_follow_up", "payload": {}},
        ]
        snapshot: dict[str, object] = {}
        for command in commands:
            result = client.post(f"/api/rooms/{room_id}/commands", headers=headers, json=command)
            result.raise_for_status()
            snapshot = result.json()["snapshot"]

        stage_state = snapshot["stage_state"]
        if not isinstance(stage_state, dict):
            raise RuntimeError("完成快照缺少 stage_state")
        postcard_id = stage_state["postcard_id"]
        postcard = client.get(f"/api/postcards/{postcard_id}", headers=headers)
        postcard.raise_for_status()
        print(
            {
                "username": username,
                "room_id": room_id,
                "final_stage": snapshot["stage"],
                "postcard_id": postcard_id,
                "has_image": postcard.json()["artwork"]["image_url"] is not None,
            }
        )


if __name__ == "__main__":
    main()
