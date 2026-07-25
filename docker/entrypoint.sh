#!/bin/sh
set -e

echo "[entrypoint] running alembic upgrade head..."
uv run alembic upgrade head

echo "[entrypoint] starting uvicorn on 0.0.0.0:8083..."
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8083 --proxy-headers --forwarded-allow-ips='*'
