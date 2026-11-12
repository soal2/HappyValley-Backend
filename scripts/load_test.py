"""并发压测脚本（仅用于 MODE=test 本地环境，不触发付费模型调用）。

用法：
    .venv/bin/python -m scripts.load_test [--base-url http://127.0.0.1:8083] [--users 60]

阶段：
    1. /health 基线吞吐（并发 1/10/50/100）
    2. 并发注册 + 登录（Argon2 CPU 密集路径）
    3. 并发完善资料
    4. 容量竞争：N 用户同时 POST /api/activities，校验房间上限与 FIFO
    5. 同一用户重复提交竞争：只允许一个席位
    6. 命令幂等：同一 command_id 并发重复提交 + 不同命令并发竞争房间锁
    7. WebSocket 并发连接与 4001 接管
    8. 活跃房间下的混合轮询压力（DB 连接池）
    9. 清理：exit 所有房间、取消排队
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import websockets
from dotenv import dotenv_values

RUN_ID = uuid.uuid4().hex[:8]
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
RESULTS: dict[str, Any] = {"run_id": RUN_ID, "phases": {}}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(name: str, samples: list[tuple[str, float]], wall: float) -> dict[str, Any]:
    latencies = [dt for _, dt in samples]
    codes = Counter(code for code, _ in samples)
    ok = sum(n for code, n in codes.items() if code.startswith("2"))
    summary = {
        "requests": len(samples),
        "ok_2xx": ok,
        "status_counts": dict(codes),
        "wall_seconds": round(wall, 3),
        "rps": round(len(samples) / wall, 1) if wall > 0 else 0,
        "latency_ms": {
            "p50": round(percentile(latencies, 50) * 1000, 1),
            "p95": round(percentile(latencies, 95) * 1000, 1),
            "p99": round(percentile(latencies, 99) * 1000, 1),
            "max": round(max(latencies) * 1000, 1) if latencies else 0,
        },
    }
    RESULTS["phases"][name] = summary
    print(f"\n== {name} ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


async def worker_pool(
    concurrency: int, total: int, fn: Any, barrier: bool = False
) -> list[tuple[str, float]]:
    """concurrency 个协程分摊 total 次调用；barrier=True 时尽量同时发射。"""
    samples: list[tuple[str, float]] = []
    counter = iter(range(total))
    gate = asyncio.Barrier(concurrency) if barrier else None

    async def worker() -> None:
        if gate is not None:
            await gate.wait()
        while True:
            try:
                i = next(counter)
            except StopIteration:
                return
            t0 = time.monotonic()
            try:
                code = await fn(i)
            except Exception as exc:  # noqa: BLE001 - 压测需记录一切异常
                code = f"exc:{type(exc).__name__}"
            samples.append((str(code), time.monotonic() - t0))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return samples


async def burst(fn: Any, n: int) -> list[tuple[str, float]]:
    """n 个请求同一时刻发射（barrier 对齐）。"""
    gate = asyncio.Barrier(n)
    samples: list[tuple[str, float]] = []

    async def one(i: int) -> None:
        await gate.wait()
        t0 = time.monotonic()
        try:
            code = await fn(i)
        except Exception as exc:  # noqa: BLE001
            code = f"exc:{type(exc).__name__}"
        samples.append((str(code), time.monotonic() - t0))

    await asyncio.gather(*(one(i) for i in range(n)))
    return samples


class Client:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(60.0),
            limits=httpx.Limits(max_connections=300, max_keepalive_connections=100),
            trust_env=False,  # 本地压测不走系统代理，也避免 NO_PROXY 中 [::1] 的解析问题
        )
        self.tokens: dict[int, str] = {}
        self.user_ids: dict[int, str] = {}
        self.usernames: dict[int, str] = {}
        self.rooms: dict[int, str] = {}

    async def close(self) -> None:
        await self.http.aclose()

    async def register(self, i: int) -> str:
        username = f"lt_{RUN_ID}_{i}"
        resp = await self.http.post(
            "/api/auth/register",
            json={"username": username, "password": f"LoadTest!{RUN_ID}"},
        )
        if resp.status_code == 200:
            self.usernames[i] = username
        return str(resp.status_code)

    async def login(self, i: int) -> str:
        resp = await self.http.post(
            "/api/auth/login",
            json={"username": self.usernames[i], "password": f"LoadTest!{RUN_ID}"},
        )
        if resp.status_code == 200:
            body = resp.json()
            self.tokens[i] = body["access_token"]
            self.user_ids[i] = body["user_id"]
        return str(resp.status_code)

    def auth(self, i: int) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[i]}"}

    async def complete_profile(self, i: int) -> str:
        resp = await self.http.post(
            "/api/profile/complete",
            headers=self.auth(i),
            json={
                "mbti": "INFP",
                "gender": "保密",
                "birth_year": 1995,
                "appearance_id": "preset_01",
            },
        )
        return str(resp.status_code)

    async def start_activity(self, i: int) -> str:
        resp = await self.http.post(
            "/api/activities", headers=self.auth(i), json={"mood": "今天有点累，想画画"}
        )
        if resp.status_code == 201:
            self.rooms[i] = resp.json()["room_id"]
        return str(resp.status_code)

    async def command(self, i: int, room_id: str, command_id: str, ctype: str) -> str:
        resp = await self.http.post(
            f"/api/rooms/{room_id}/commands",
            headers=self.auth(i),
            json={"command_id": command_id, "type": ctype, "payload": {}},
        )
        if resp.status_code == 200:
            return f"200:{resp.json().get('status')}"
        return str(resp.status_code)


async def db_conn() -> asyncpg.Connection:
    env = await asyncio.to_thread(dotenv_values, ENV_PATH)
    url = str(env.get("DATABASE_URL", "")).replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(url)


async def phase_health(client: Client) -> None:
    async def get_health(_: int) -> str:
        resp = await client.http.get("/health")
        return str(resp.status_code)

    for concurrency in (1, 10, 50, 100):
        total = max(300, concurrency * 20)
        t0 = time.monotonic()
        samples = await worker_pool(concurrency, total, get_health)
        summarize(f"health_c{concurrency}", samples, time.monotonic() - t0)


async def phase_auth(client: Client, n_users: int) -> None:
    t0 = time.monotonic()
    samples = await burst(client.register, n_users)
    summarize(f"register_burst_{n_users}", samples, time.monotonic() - t0)

    t0 = time.monotonic()
    samples = await burst(client.login, n_users)
    summarize(f"login_burst_{n_users}", samples, time.monotonic() - t0)

    t0 = time.monotonic()
    samples = await burst(client.complete_profile, n_users)
    summarize(f"profile_burst_{n_users}", samples, time.monotonic() - t0)


async def phase_capacity_race(client: Client, n_users: int) -> None:
    t0 = time.monotonic()
    samples = await burst(client.start_activity, n_users)
    summary = summarize(f"capacity_race_{n_users}", samples, time.monotonic() - t0)

    conn = await db_conn()
    try:
        active_rooms = await conn.fetchval(
            "SELECT count(*) FROM activity_rooms WHERE status='active'"
        )
        queue_rows = await conn.fetch(
            "SELECT user_id, status, position FROM queue_entries ORDER BY position"
        )
        per_user = Counter(row["user_id"] for row in queue_rows)
        dup_entries = {u: c for u, c in per_user.items() if c > 1}
        positions = [row["position"] for row in queue_rows if row["status"] == "queued"]
        started = summary["status_counts"].get("201", 0)
        queued = summary["status_counts"].get("202", 0)
        checks = {
            "active_rooms_in_db": active_rooms,
            "started_201": started,
            "queued_202": queued,
            "queue_entries_in_db": len(queue_rows),
            "duplicate_queue_entries_per_user": dup_entries,
            "queued_positions": positions,
            "max_active_rooms_limit": 6,
            "invariant_rooms_le_limit": active_rooms <= 6,
            "invariant_started_plus_queued_eq_users": started + queued == n_users,
            "invariant_no_duplicate_entries": not dup_entries,
            "invariant_fifo_positions_unique": len(positions) == len(set(positions)),
        }
        RESULTS["phases"]["capacity_race_checks"] = checks
        print("\n== capacity_race_checks ==")
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    finally:
        await conn.close()


async def phase_same_user_race(client: Client, n_users: int) -> None:
    idx = n_users  # 额外新用户
    await client.register(idx)
    await client.login(idx)
    await client.complete_profile(idx)

    async def duplicate_start(_: int) -> str:
        return await client.start_activity(idx)

    t0 = time.monotonic()
    samples = await burst(duplicate_start, 12)
    summarize("same_user_race_12", samples, time.monotonic() - t0)

    conn = await db_conn()
    try:
        user_id = None
        row = await conn.fetchrow("SELECT id FROM users WHERE username=$1", client.usernames[idx])
        if row:
            user_id = row["id"]
        entries = await conn.fetchval(
            "SELECT count(*) FROM queue_entries WHERE user_id=$1", user_id
        )
        rooms = await conn.fetchval(
            "SELECT count(*) FROM activity_rooms WHERE user_id=$1 AND status='active'",
            user_id,
        )
        checks = {
            "queue_entries_for_user": entries,
            "active_rooms_for_user": rooms,
            "invariant_single_occupancy": entries + rooms == 1,
        }
        RESULTS["phases"]["same_user_race_checks"] = checks
        print("\n== same_user_race_checks ==")
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    finally:
        await conn.close()


async def phase_commands(client: Client) -> None:
    if not client.rooms:
        print("!! 没有可用房间，跳过命令测试")
        return
    owner, room_id = next(iter(client.rooms.items()))

    # --- 1) 真实业务命令的幂等性：等到真人绘画轮，并发重复同一 command_id ---
    turn_index = None
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        resp = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(owner))
        if resp.status_code == 200:
            snap = resp.json()
            state = snap.get("stage_state", {})
            if snap.get("stage") == "PAINTING_TURN":
                idx = int(state.get("painting_turn_index", -1))
                order = state.get("speaking_order", [])
                members = {str(m.get("id")): m for m in state.get("members", [])}
                if 0 <= idx < len(order):
                    speaker = members.get(str(order[idx]), {})
                    if (
                        speaker.get("type") == "human"
                        and str(speaker.get("id")) == client.user_ids[owner]
                    ):
                        turn_index = idx
                        break
        await asyncio.sleep(0.3)

    if turn_index is None:
        print("!! 45 秒内未等到真人绘画轮，跳过幂等测试")
        RESULTS["phases"]["idempotency_checks"] = {"skipped": "human turn not reached"}
    else:
        shared_id = f"dup-paint-{RUN_ID}"

        async def dup_painting(_: int) -> str:
            resp = await client.http.post(
                f"/api/rooms/{room_id}/commands",
                headers=client.auth(owner),
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
        samples = await burst(dup_painting, 12)
        summarize("idempotent_painting_duplicate_12", samples, time.monotonic() - t0)

        resp = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(owner))
        state = resp.json().get("stage_state", {}) if resp.status_code == 200 else {}
        processed = state.get("processed_command_ids", [])
        human_messages = [
            m
            for m in resp.json().get("messages", [])
            if m.get("sender_type") == "human" and m.get("stage") == "PAINTING_TURN"
        ]
        checks = {
            "command_id_in_processed_once": processed.count(shared_id) == 1,
            "human_painting_messages": len(human_messages),
            "invariant_single_message": len(human_messages) == 1,
        }
        RESULTS["phases"]["idempotency_checks"] = checks
        print("\n== idempotency_checks ==")
        print(json.dumps(checks, ensure_ascii=False, indent=2))

    # --- 2) heartbeat 并发（轻量路径，不走房间锁/幂等去重，作为对照） ---
    async def distinct_heartbeat(i: int) -> str:
        return await client.command(owner, room_id, f"hb-{RUN_ID}-{i}", "heartbeat")

    t0 = time.monotonic()
    samples = await burst(distinct_heartbeat, 30)
    summarize("heartbeat_concurrent_30", samples, time.monotonic() - t0)

    snapshot = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(owner))
    checks = {
        "snapshot_status": snapshot.status_code,
        "revision": snapshot.json().get("revision") if snapshot.status_code == 200 else None,
    }
    RESULTS["phases"]["command_checks"] = checks
    print("\n== command_checks ==")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


async def phase_websocket(client: Client, base_url: str) -> None:
    if not client.rooms:
        print("!! 没有可用房间，跳过 WebSocket 测试")
        return
    owner, room_id = next(iter(client.rooms.items()))
    ws_url = base_url.replace("http", "ws", 1) + f"/api/rooms/{room_id}/ws"
    token = client.tokens[owner]
    outcomes: list[str] = []

    async def one_connection(k: int) -> None:
        try:
            async with websockets.connect(f"{ws_url}?token={token}") as ws:
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=10)
                except websockets.ConnectionClosed as closed:
                    code = closed.rcvd.code if closed.rcvd else "?"
                    outcomes.append(f"conn{k}:closed_before_snapshot:{code}")
                    return
                msg = json.loads(first)
                if msg.get("type") != "snapshot":
                    outcomes.append(f"conn{k}:unexpected_first:{msg.get('type')}")
                    return
                # 持续接收：被接管时以 4001 关闭；存活则直到 2 秒空闲
                while True:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=2.0)
                    except TimeoutError:
                        outcomes.append(f"conn{k}:survived")
                        return
                    except websockets.ConnectionClosed as closed:
                        code = closed.rcvd.code if closed.rcvd else "?"
                        outcomes.append(f"conn{k}:closed:{code}")
                        return
        except Exception as exc:  # noqa: BLE001
            outcomes.append(f"conn{k}:exc:{type(exc).__name__}")

    t0 = time.monotonic()
    await asyncio.gather(*(one_connection(k) for k in range(8)))
    wall = time.monotonic() - t0
    survived = sum(1 for o in outcomes if o.endswith(":survived"))
    takeover = sum(1 for o in outcomes if o.endswith(":closed:4001"))
    summary = {
        "connections": 8,
        "wall_seconds": round(wall, 3),
        "outcomes": outcomes,
        "survived": survived,
        "closed_4001_takeover": takeover,
        "invariant_single_survivor": survived == 1 and takeover == 7,
    }
    RESULTS["phases"]["websocket_takeover_8"] = summary
    print("\n== websocket_takeover_8 ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def phase_mixed_polling(client: Client, seconds: float = 8.0) -> None:
    if not client.rooms:
        print("!! 没有可用房间，跳过混合轮询")
        return
    items = list(client.rooms.items())
    deadline = time.monotonic() + seconds
    samples: list[tuple[str, float]] = []

    async def poller(k: int) -> None:
        owner, room_id = items[k % len(items)]
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            try:
                resp = await client.http.get(f"/api/rooms/{room_id}", headers=client.auth(owner))
                code = str(resp.status_code)
            except Exception as exc:  # noqa: BLE001
                code = f"exc:{type(exc).__name__}"
            samples.append((code, time.monotonic() - t0))

    t0 = time.monotonic()
    await asyncio.gather(*(poller(k) for k in range(40)))
    summarize("mixed_snapshot_polling_40x8s", samples, time.monotonic() - t0)


async def phase_cleanup(client: Client) -> None:
    # 以 DB 为准清理本轮所有房间（含客户端超时但服务端已建房的孤儿房间）
    conn = await db_conn()
    try:
        rows = await conn.fetch(
            """
            SELECT r.id AS room_id, u.username FROM activity_rooms r
            JOIN users u ON r.user_id = u.id
            WHERE r.status='active' AND u.username LIKE $1
            """,
            f"lt_{RUN_ID}_%",
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
    for i in list(client.tokens):
        try:
            await client.http.delete("/api/queue/me", headers=client.auth(i))
        except Exception:  # noqa: BLE001
            pass
    conn = await db_conn()
    try:
        active = await conn.fetchval(
            """
            SELECT count(*) FROM activity_rooms r JOIN users u ON r.user_id=u.id
            WHERE r.status='active' AND u.username LIKE $1
            """,
            f"lt_{RUN_ID}_%",
        )
        queued = await conn.fetchval(
            """
            SELECT count(*) FROM queue_entries q JOIN users u ON q.user_id=u.id
            WHERE u.username LIKE $1
            """,
            f"lt_{RUN_ID}_%",
        )
        checks = {"active_rooms_after_cleanup": active, "queue_entries_after_cleanup": queued}
        RESULTS["phases"]["cleanup_checks"] = checks
        print("\n== cleanup_checks ==")
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    finally:
        await conn.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="心晴谷后端并发压测（MODE=test）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8083")
    parser.add_argument("--users", type=int, default=60)
    parser.add_argument("--out", default="/tmp/hv_loadtest_results.json")
    args = parser.parse_args()

    client = Client(args.base_url)
    health = await client.http.get("/health")
    if health.status_code != 200:
        raise SystemExit(f"服务不可用: {health.status_code}")
    print(f"目标: {args.base_url} | 用户数: {args.users} | run_id: {RUN_ID}")

    try:
        await phase_health(client)
        await phase_auth(client, args.users)
        await phase_capacity_race(client, args.users)
        await phase_same_user_race(client, args.users)
        await phase_commands(client)
        await phase_websocket(client, args.base_url)
        await phase_mixed_polling(client)
        await phase_cleanup(client)
    finally:
        await client.close()

    await asyncio.to_thread(
        Path(args.out).write_text, json.dumps(RESULTS, ensure_ascii=False, indent=2)
    )
    print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
