"""补充压测：容量竞争阈值扫描 + 无干扰幂等验证 + WS 接管复测。

仅记录问题，不修改应用代码。复用 scripts.load_test 的客户端与工具函数。
用法：.venv/bin/python -m scripts.load_test_followup [--base-url http://127.0.0.1:8083]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import httpx

from scripts.load_test import RESULTS, RUN_ID, Client, burst, db_conn, summarize

FOLLOWUP: dict[str, object] = {"run_id": RUN_ID, "sweep": [], "idempotency": {}, "websocket": {}}


async def cleanup_users(client: Client, indices: list[int]) -> None:
    """以 DB 为准清理指定用户的全部房间与排队条目。"""
    names = [client.usernames[i] for i in indices if i in client.usernames]
    if not names:
        return
    conn = await db_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT r.id AS room_id, u.username FROM activity_rooms r
            JOIN users u ON r.user_id = u.id
            WHERE r.status='active' AND u.username = ANY($1::text[])
            """,
            names,
        )
    finally:
        await conn.close()
    by_name = {name: i for i, name in client.usernames.items()}
    for row in rows:
        i = by_name.get(row["username"])
        if i is None or i not in client.tokens:
            continue
        try:
            await client.command(i, row["room_id"], f"exit-{RUN_ID}-{row['room_id']}", "exit")
        except Exception:  # noqa: BLE001
            pass
    for i in indices:
        if i in client.tokens:
            try:
                await client.http.delete("/api/queue/me", headers=client.auth(i))
            except Exception:  # noqa: BLE001
                pass


async def provision(client: Client, indices: list[int]) -> None:
    gate = asyncio.Barrier(len(indices))

    async def one(i: int) -> None:
        await gate.wait()
        await client.register(i)
        await client.login(i)
        await client.complete_profile(i)

    await asyncio.gather(*(one(i) for i in indices))


async def sweep_level(client: Client, level: int, base_index: int) -> dict[str, object]:
    indices = list(range(base_index, base_index + level))
    await provision(client, indices)

    async def start(i: int) -> str:
        return await client.start_activity(i)

    gate = asyncio.Barrier(level)
    samples: list[tuple[str, float]] = []

    async def one(i: int) -> None:
        await gate.wait()
        t0 = time.monotonic()
        try:
            code = await start(i)
        except Exception as exc:  # noqa: BLE001
            code = f"exc:{type(exc).__name__}"
        samples.append((str(code), time.monotonic() - t0))

    t0 = time.monotonic()
    await asyncio.gather(*(one(i) for i in indices))
    wall = time.monotonic() - t0
    summary = summarize(f"sweep_capacity_c{level}", samples, wall)

    await asyncio.sleep(2)  # 等待服务端迟到的建房完成
    conn = await db_conn()
    try:
        active = await conn.fetchval("SELECT count(*) FROM activity_rooms WHERE status='active'")
        entries = await conn.fetchval("SELECT count(*) FROM queue_entries")
    finally:
        await conn.close()
    started = summary["status_counts"].get("201", 0)
    queued = summary["status_counts"].get("202", 0)
    errors = sum(n for code, n in summary["status_counts"].items() if not code.startswith("2"))
    record = {
        "concurrency": level,
        "started_201": started,
        "queued_202": queued,
        "errors": errors,
        "status_counts": summary["status_counts"],
        "wall_seconds": round(wall, 2),
        "active_rooms_db": active,
        "queue_entries_db": entries,
        "passed": errors == 0 and started + queued == level and active <= 6,
    }
    FOLLOWUP["sweep"].append(record)  # type: ignore[union-attr]
    print(f"\n== sweep_level_c{level} ==")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    await cleanup_users(client, indices)
    return record


async def clean_idempotency(client: Client, idx: int) -> None:
    await provision(client, [idx])
    code = await client.start_activity(idx)
    if code != "201" or idx not in client.rooms:
        FOLLOWUP["idempotency"] = {"skipped": f"start_activity returned {code}"}
        return
    room_id = client.rooms[idx]

    turn_index = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        resp = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(idx))
        if resp.status_code == 200:
            snap = resp.json()
            state = snap.get("stage_state", {})
            if snap.get("stage") == "PAINTING_TURN":
                i = int(state.get("painting_turn_index", -1))
                order = state.get("speaking_order", [])
                members = {str(m.get("id")): m for m in state.get("members", [])}
                if 0 <= i < len(order):
                    speaker = members.get(str(order[i]), {})
                    if (
                        speaker.get("type") == "human"
                        and str(speaker.get("id")) == client.user_ids[idx]
                    ):
                        turn_index = i
                        break
        await asyncio.sleep(0.2)
    if turn_index is None:
        FOLLOWUP["idempotency"] = {"skipped": "human turn not reached in 45s"}
        return

    shared_id = f"dup-paint-clean-{RUN_ID}"

    async def dup(_: int) -> str:
        resp = await client.http.post(
            f"/api/rooms/{room_id}/commands",
            headers=client.auth(idx),
            json={
                "command_id": shared_id,
                "type": "submit_painting_turn",
                "payload": {"turn_index": turn_index, "text": "山脚下的一棵小树"},
            },
        )
        if resp.status_code == 200:
            return f"200:{resp.json().get('status')}"
        return str(resp.status_code)

    t0 = time.monotonic()
    samples = await burst(dup, 12)
    summarize("clean_idempotent_painting_12", samples, time.monotonic() - t0)

    resp = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(idx))
    snap = resp.json()
    state = snap.get("stage_state", {})
    processed = state.get("processed_command_ids", [])
    human_messages = [
        m
        for m in snap.get("messages", [])
        if m.get("sender_type") == "human" and m.get("stage") == "PAINTING_TURN"
    ]
    FOLLOWUP["idempotency"] = {
        "status_counts": dict(Counter(c for c, _ in samples)),
        "command_id_in_processed_once": processed.count(shared_id) == 1,
        "human_painting_messages": len(human_messages),
        "invariant_single_message": len(human_messages) == 1,
        "revision": snap.get("revision"),
    }
    print("\n== clean_idempotency_checks ==")
    print(json.dumps(FOLLOWUP["idempotency"], ensure_ascii=False, indent=2))


async def ws_takeover(client: Client, idx: int, base_url: str) -> None:
    import websockets

    if idx not in client.rooms:
        FOLLOWUP["websocket"] = {"skipped": "no room"}
        return
    room_id = client.rooms[idx]
    ws_url = base_url.replace("http", "ws", 1) + f"/api/rooms/{room_id}/ws"
    token = client.tokens[idx]
    outcomes: list[str] = []

    async def one(k: int) -> None:
        try:
            async with websockets.connect(f"{ws_url}?token={token}") as ws:
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=10)
                except websockets.ConnectionClosed as closed:
                    code = closed.rcvd.code if closed.rcvd else "?"
                    outcomes.append(f"closed_before_snapshot:{code}")
                    return
                if json.loads(first).get("type") != "snapshot":
                    outcomes.append("unexpected_first_message")
                    return
                while True:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except TimeoutError:
                        outcomes.append("survived")
                        return
                    except websockets.ConnectionClosed as closed:
                        code = closed.rcvd.code if closed.rcvd else "?"
                        outcomes.append(f"closed:{code}")
                        return
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"exc:{type(exc).__name__}")

    await asyncio.gather(*(one(k) for k in range(8)))
    survived = outcomes.count("survived")
    takeover = sum(1 for o in outcomes if o.endswith(":4001"))
    FOLLOWUP["websocket"] = {
        "connections": 8,
        "outcomes": outcomes,
        "survived": survived,
        "closed_4001_total": takeover,
        "invariant_single_survivor": survived == 1 and takeover == 7,
    }
    print("\n== ws_takeover_clean ==")
    print(json.dumps(FOLLOWUP["websocket"], ensure_ascii=False, indent=2))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8083")
    parser.add_argument("--out", default="/tmp/hv_loadtest_followup.json")
    args = parser.parse_args()

    client = Client(args.base_url)
    client.http.timeout = httpx.Timeout(45.0)
    try:
        base = 1000
        for level in (10, 15, 16, 24):
            await sweep_level(client, level, base)
            base += level
        await clean_idempotency(client, 9000)
        await ws_takeover(client, 9000, args.base_url)
        await cleanup_users(client, [9000])
    finally:
        await client.close()

    RESULTS["followup"] = FOLLOWUP
    await asyncio.to_thread(
        Path(args.out).write_text, json.dumps(RESULTS, ensure_ascii=False, indent=2)
    )
    print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
