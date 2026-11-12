"""线上单房间并发压测（范围经用户确认：只读梯度 + 1 个真实房间，不做容量竞争）。

安全护栏：
    - 仅注册 2 个测试账号（lt_prod_<runid>_a/b）
    - 只读压测走 /api/postcards（纯读、无容量锁），梯度 1/10/30/50
    - 只创建 1 个房间；若返回 202 排队（线上容量已满）立即取消并跳过房间阶段
    - 房间阶段结束后立即 exit，并验证活动已终止、排队已清空
    - 不做 60 并发建房竞争，避免触发已知 P0 死锁影响真实用户

用法：
    .venv/bin/python -m scripts.load_test_prod [--base-url https://happy.shikongduihua.com]
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

import httpx
import websockets

RUN_ID = uuid.uuid4().hex[:8]
RESULTS: dict[str, Any] = {"run_id": RUN_ID, "target": None, "phases": {}}


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(name: str, samples: list[tuple[str, float]], wall: float) -> dict[str, Any]:
    latencies = [dt for _, dt in samples]
    codes = Counter(code for code, _ in samples)
    summary = {
        "requests": len(samples),
        "ok_2xx": sum(n for c, n in codes.items() if c.startswith("2")),
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


async def worker_pool(concurrency: int, total: int, fn: Any) -> list[tuple[str, float]]:
    samples: list[tuple[str, float]] = []
    counter = iter(range(total))

    async def worker() -> None:
        while True:
            try:
                i = next(counter)
            except StopIteration:
                return
            t0 = time.monotonic()
            try:
                code = await fn(i)
            except Exception as exc:  # noqa: BLE001
                code = f"exc:{type(exc).__name__}"
            samples.append((str(code), time.monotonic() - t0))

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return samples


async def burst(fn: Any, n: int) -> list[tuple[str, float]]:
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


class ProdClient:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(120.0),
            limits=httpx.Limits(max_connections=120, max_keepalive_connections=60),
            trust_env=False,
        )
        self.password = f"ProdLoad!{RUN_ID}"
        self.names = {"a": f"lt_prod_{RUN_ID}_a", "b": f"lt_prod_{RUN_ID}_b"}
        self.tokens: dict[str, str] = {}
        self.user_ids: dict[str, str] = {}
        self.room_id: str | None = None

    async def close(self) -> None:
        await self.http.aclose()

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    async def setup_user(self, who: str) -> None:
        resp = await self.http.post(
            "/api/auth/register",
            json={"username": self.names[who], "password": self.password},
        )
        if resp.status_code != 200:
            raise SystemExit(f"注册失败 {who}: {resp.status_code} {resp.text[:200]}")
        resp = await self.http.post(
            "/api/auth/login",
            json={"username": self.names[who], "password": self.password},
        )
        if resp.status_code != 200:
            raise SystemExit(f"登录失败 {who}: {resp.status_code}")
        body = resp.json()
        self.tokens[who] = body["access_token"]
        self.user_ids[who] = body["user_id"]
        resp = await self.http.post(
            "/api/profile/complete",
            headers=self.auth(who),
            json={
                "mbti": "INFP",
                "gender": "保密",
                "birth_year": 1995,
                "appearance_id": "preset_01",
            },
        )
        if resp.status_code != 200:
            raise SystemExit(f"完善资料失败 {who}: {resp.status_code}")

    async def get(self, who: str, path: str) -> str:
        resp = await self.http.get(path, headers=self.auth(who))
        return str(resp.status_code)

    async def command(
        self, who: str, command_id: str, ctype: str, payload: dict
    ) -> tuple[str, dict]:
        resp = await self.http.post(
            f"/api/rooms/{self.room_id}/commands",
            headers=self.auth(who),
            json={"command_id": command_id, "type": ctype, "payload": payload},
        )
        if resp.status_code == 200:
            return f"200:{resp.json().get('status')}", resp.json()
        return str(resp.status_code), {}


async def phase_read_sweep(client: ProdClient) -> None:
    async def postcards(_: int) -> str:
        return await client.get("a", "/api/postcards")

    for concurrency in (1, 10, 30, 50):
        total = {1: 100, 10: 200, 30: 450, 50: 750}[concurrency]
        t0 = time.monotonic()
        samples = await worker_pool(concurrency, total, postcards)
        summarize(f"prod_postcards_c{concurrency}", samples, time.monotonic() - t0)

    async def queue_me(_: int) -> str:
        return await client.get("b", "/api/queue/me")

    t0 = time.monotonic()
    samples = await worker_pool(10, 100, queue_me)
    summarize("prod_queue_me_c10", samples, time.monotonic() - t0)


async def phase_create_room(client: ProdClient) -> bool:
    resp = await client.http.post(
        "/api/activities", headers=client.auth("a"), json={"mood": "今天心情平静，想画一幅画"}
    )
    try:
        body = resp.json()
    except ValueError:
        body = {"raw": resp.text[:200]}
    RESULTS["phases"]["create_room"] = {"status_code": resp.status_code, "body": body}
    print("\n== create_room ==")
    print(json.dumps(RESULTS["phases"]["create_room"], ensure_ascii=False, indent=2))
    if resp.status_code == 201:
        client.room_id = body["room_id"]
        return True
    # 202 = 线上容量已满：立即取消排队，绝不占用真实用户席位
    if resp.status_code == 202:
        await client.http.delete("/api/queue/me", headers=client.auth("a"))
        print("!! 线上房间容量已满，已取消排队，跳过房间阶段")
    return False


async def wait_human_turn(client: ProdClient, timeout_s: float = 240.0) -> int | None:
    deadline = time.monotonic() + timeout_s
    last_stage = None
    while time.monotonic() < deadline:
        resp = await client.http.get(f"/api/rooms/{client.room_id}", headers=client.auth("a"))
        if resp.status_code == 200:
            snap = resp.json()
            state = snap.get("stage_state", {})
            last_stage = snap.get("stage")
            if snap.get("stage") == "PAINTING_TURN":
                idx = int(state.get("painting_turn_index", -1))
                order = state.get("speaking_order", [])
                members = {str(m.get("id")): m for m in state.get("members", [])}
                if 0 <= idx < len(order):
                    speaker = members.get(str(order[idx]), {})
                    if (
                        speaker.get("type") == "human"
                        and str(speaker.get("id")) == client.user_ids["a"]
                    ):
                        return idx
            if snap.get("status") != "active":
                print(f"!! 房间已非活跃: {snap.get('status')}")
                return None
        await asyncio.sleep(1.0)
    print(f"!! {timeout_s}s 内未等到真人绘画轮，最后阶段: {last_stage}")
    return None


async def phase_room_commands(client: ProdClient) -> None:
    turn_index = await wait_human_turn(client)
    if turn_index is None:
        RESULTS["phases"]["idempotency"] = {"skipped": "human turn not reached"}
        return

    shared_id = f"dup-paint-prod-{RUN_ID}"

    async def dup(_: int) -> str:
        code, _body = await client.command(
            "a",
            shared_id,
            "submit_painting_turn",
            {"turn_index": turn_index, "text": "山脚下的一棵小树"},
        )
        return code

    t0 = time.monotonic()
    samples = await burst(dup, 12)
    summarize("prod_idempotent_painting_12", samples, time.monotonic() - t0)

    resp = await client.http.get(f"/api/rooms/{client.room_id}", headers=client.auth("a"))
    snap = resp.json() if resp.status_code == 200 else {}
    human_messages = [
        m
        for m in snap.get("messages", [])
        if m.get("sender_type") == "human" and m.get("stage") == "PAINTING_TURN"
    ]
    checks = {
        "human_painting_messages": len(human_messages),
        "invariant_single_message": len(human_messages) == 1,
        "stage_after": snap.get("stage"),
        "revision": snap.get("revision"),
    }
    RESULTS["phases"]["idempotency_checks"] = checks
    print("\n== prod_idempotency_checks ==")
    print(json.dumps(checks, ensure_ascii=False, indent=2))

    async def heartbeat(i: int) -> str:
        code, _ = await client.command("a", f"hb-prod-{RUN_ID}-{i}", "heartbeat", {})
        return code

    t0 = time.monotonic()
    samples = await burst(heartbeat, 20)
    summarize("prod_heartbeat_20", samples, time.monotonic() - t0)


async def phase_ws(client: ProdClient, base_url: str) -> None:
    ws_url = base_url.replace("http", "ws", 1) + f"/api/rooms/{client.room_id}/ws"
    token = client.tokens["a"]
    outcomes: list[str] = []

    async def one(k: int) -> None:
        try:
            async with websockets.connect(f"{ws_url}?token={token}", open_timeout=20) as ws:
                try:
                    first = await asyncio.wait_for(ws.recv(), timeout=15)
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
    summary = {
        "connections": 8,
        "outcomes": outcomes,
        "survived": survived,
        "closed_4001_total": takeover,
        "invariant_single_survivor": survived == 1 and takeover == 7,
    }
    RESULTS["phases"]["ws_takeover"] = summary
    print("\n== prod_ws_takeover ==")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def phase_polling(client: ProdClient, seconds: float = 6.0) -> None:
    deadline = time.monotonic() + seconds
    samples: list[tuple[str, float]] = []

    async def poller(_: int) -> None:
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            try:
                code = await client.get("a", f"/api/rooms/{client.room_id}")
            except Exception as exc:  # noqa: BLE001
                code = f"exc:{type(exc).__name__}"
            samples.append((code, time.monotonic() - t0))

    t0 = time.monotonic()
    await asyncio.gather(*(poller(k) for k in range(20)))
    summarize("prod_snapshot_polling_20x6s", samples, time.monotonic() - t0)


async def phase_cleanup(client: ProdClient) -> None:
    checks: dict[str, Any] = {}
    if client.room_id:
        code, _ = await client.command("a", f"exit-prod-{RUN_ID}", "exit", {})
        checks["exit_command"] = code
        resp = await client.http.get(f"/api/rooms/{client.room_id}", headers=client.auth("a"))
        checks["room_status_after_exit"] = (
            resp.json().get("status") if resp.status_code == 200 else resp.status_code
        )
    for who in ("a", "b"):
        await client.http.delete("/api/queue/me", headers=client.auth(who))
        resp = await client.http.get("/api/rooms/active", headers=client.auth(who))
        checks[f"active_room_{who}"] = resp.status_code  # 期望 404
        resp = await client.http.get("/api/queue/me", headers=client.auth(who))
        checks[f"queue_{who}"] = (
            resp.json().get("status") if resp.status_code == 200 else resp.status_code
        )
    checks["invariant_clean"] = (
        checks.get("active_room_a") == 404
        and checks.get("active_room_b") == 404
        and checks.get("queue_a") == "none"
        and checks.get("queue_b") == "none"
    )
    RESULTS["phases"]["cleanup_checks"] = checks
    print("\n== prod_cleanup_checks ==")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


async def main() -> None:
    parser = argparse.ArgumentParser(description="线上单房间并发压测（不做容量竞争）")
    parser.add_argument("--base-url", default="https://happy.shikongduihua.com")
    parser.add_argument("--out", default="/tmp/hv_loadtest_prod.json")
    args = parser.parse_args()
    RESULTS["target"] = args.base_url

    client = ProdClient(args.base_url)
    try:
        await client.setup_user("a")
        await client.setup_user("b")
        print(f"测试账号就绪: {client.names}")
        await phase_read_sweep(client)
        if await phase_create_room(client):
            try:
                await phase_room_commands(client)
                await phase_ws(client, args.base_url)
                await phase_polling(client)
            finally:
                await phase_cleanup(client)
        else:
            await phase_cleanup(client)
    finally:
        await client.close()

    await asyncio.to_thread(
        Path(args.out).write_text, json.dumps(RESULTS, ensure_ascii=False, indent=2)
    )
    print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
