#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="${HAPPYVALLEY_DEPLOY_ROOT:-/root/happyvalley-deploy}"
COMPOSE_FILE="$DEPLOY_ROOT/deploy/docker-compose.prod.yml"
RELEASES_DIR="$DEPLOY_ROOT/releases"

fail() { echo "rollback: $*" >&2; exit 1; }
[[ $# -eq 2 ]] || fail "usage: $0 <frontend|backend> <commit>"
service="$1"
commit="$2"
[[ "$service" == frontend || "$service" == backend ]] || fail "service must be frontend or backend"
[[ "$commit" =~ ^[0-9a-f]{7,64}$ ]] || fail "commit must be a hexadecimal SHA"

image="happyvalley-${service}:rollback-${commit}"
prod_image="happyvalley-${service}:prod"
docker image inspect "$image" >/dev/null || fail "rollback image not found: $image"
docker tag "$image" "$prod_image"
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$service"

container="hv_${service}"
for attempt in $(seq 1 30); do
  case "$service" in
    backend) docker exec "$container" curl -fsS http://127.0.0.1:8083/health >/dev/null 2>&1 && break ;;
    frontend) docker exec "$container" wget -q -O /dev/null http://127.0.0.1:3000/ >/dev/null 2>&1 && break ;;
  esac
  sleep 1
done
if [[ "$service" == backend ]]; then
  docker exec "$container" curl -fsS http://127.0.0.1:8083/health >/dev/null
else
  docker exec "$container" wget -q -O /dev/null http://127.0.0.1:3000/
fi

mkdir -p "$RELEASES_DIR"
python3 - "$RELEASES_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${service}-rollback-${commit}.json" "$service" "$commit" <<'PY'
import json, sys
from datetime import datetime, timezone
with open(sys.argv[1], "w", encoding="utf-8") as fp:
    json.dump({"timestamp": datetime.now(timezone.utc).isoformat(), "service": sys.argv[2], "rollback_to": sys.argv[3], "result": "success"}, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
PY
echo "rolled back $service to $commit"
