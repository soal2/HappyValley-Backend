#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="${HAPPYVALLEY_DEPLOY_ROOT:-/root/happyvalley-deploy}"
COMPOSE_FILE="$DEPLOY_ROOT/deploy/docker-compose.prod.yml"
INCOMING_DIR="$DEPLOY_ROOT/incoming"
RELEASES_DIR="$DEPLOY_ROOT/releases"
MEDIA_DIR="$DEPLOY_ROOT/data/media"
PUBLIC_URL="${HAPPYVALLEY_PUBLIC_URL:-https://happy.shikongduihua.com}"

fail() { echo "release-image: $*" >&2; exit 1; }

service_container() {
  case "$1" in
    backend) echo hv_backend ;;
    frontend) echo hv_frontend ;;
    *) fail "unsupported service: $1" ;;
  esac
}

validate_commit() {
  [[ "$1" =~ ^[0-9a-f]{7,64}$ ]] || fail "commit must be a hexadecimal SHA"
}

check_service_health() {
  local service="$1" container status media_host_count media_container_count
  container="$(service_container "$service")"
  if ! docker inspect "$container" >/dev/null 2>&1; then
    echo "container is missing: $container" >&2
    return 1
  fi

  case "$service" in
    backend)
      if ! docker exec "$container" curl -fsS http://127.0.0.1:8083/health >/dev/null; then
        echo "backend internal health check failed" >&2
        return 1
      fi
      status="$(curl -ksS -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api/rooms/active" || true)"
      if [[ "$status" != 200 && "$status" != 401 && "$status" != 403 ]]; then
        echo "backend proxy returned HTTP $status" >&2
        return 1
      fi
      if [[ ! -d "$MEDIA_DIR" ]]; then
        echo "media directory is missing: $MEDIA_DIR" >&2
        return 1
      fi
      media_host_count="$(find "$MEDIA_DIR" -type f | wc -l | tr -d ' ')"
      media_container_count="$(docker exec "$container" sh -c 'find /app/media -type f | wc -l' | tr -d ' ')"
      if [[ "$media_host_count" != "$media_container_count" ]]; then
        echo "media mount count mismatch ($media_host_count host, $media_container_count container)" >&2
        return 1
      fi
      ;;
    frontend)
      if ! docker exec "$container" wget -q -O /dev/null http://127.0.0.1:3000/; then
        echo "frontend internal health check failed" >&2
        return 1
      fi
      status="$(curl -ksS -o /dev/null -w '%{http_code}' "$PUBLIC_URL/" || true)"
      if [[ "$status" != 200 ]]; then
        echo "public homepage returned HTTP $status" >&2
        return 1
      fi
      ;;
  esac
}

wait_for_service() {
  local service="$1" container attempt
  container="$(service_container "$service")"
  for attempt in $(seq 1 30); do
    case "$service" in
      backend) docker exec "$container" curl -fsS http://127.0.0.1:8083/health >/dev/null 2>&1 && return 0 ;;
      frontend) docker exec "$container" wget -q -O /dev/null http://127.0.0.1:3000/ >/dev/null 2>&1 && return 0 ;;
    esac
    sleep 1
  done
  echo "$service did not become ready within 30 seconds" >&2
  return 1
}

write_record() {
  local service="$1" commit="$2" result="$3" previous_commit="$4" detail="$5"
  mkdir -p "$RELEASES_DIR"
  local stamp record
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  record="$RELEASES_DIR/${stamp}-${service}-${commit}.json"
  python3 - "$record" "$service" "$commit" "$result" "$previous_commit" "$detail" <<'PY'
import json, sys
from datetime import datetime, timezone
path, service, commit, result, previous_commit, detail = sys.argv[1:]
with open(path, "w", encoding="utf-8") as fp:
    json.dump({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "commit": commit,
        "result": result,
        "previous_commit": previous_commit or None,
        "detail": detail,
    }, fp, ensure_ascii=False, indent=2)
    fp.write("\n")
PY
}

cleanup_old_images() {
  local service="$1" keep
  keep="$(python3 - "$RELEASES_DIR" "$service" <<'PY'
import json, pathlib, sys
directory = pathlib.Path(sys.argv[1])
service = sys.argv[2]
commits = []
for path in sorted(directory.glob(f"*-{service}-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        continue
    if data.get("service") == service and data.get("result") == "success" and isinstance(data.get("commit"), str):
        if data["commit"] not in commits:
            commits.append(data["commit"])
print("\n".join(commits[:3]))
PY
)"
  while IFS= read -r tag; do
    [[ -n "$tag" ]] || continue
    if ! grep -Fxq "$tag" <<<"$keep"; then
      docker image rm "happyvalley-${service}:$tag" >/dev/null 2>&1 || true
    fi
  done < <(docker images "happyvalley-${service}" --format '{{.Tag}}' | grep -E '^[0-9a-f]{7,64}$' || true)
}

release_one() {
  local service="$1" commit="$2" archive="$3" image="happyvalley-${service}:${commit}"
  local prod_image="happyvalley-${service}:prod" previous_id="" previous_commit="unknown"
  local rollback_tag="" detail="released"

  [[ -f "$archive" ]] || fail "image archive not found: $archive"
  docker load -i "$archive" >/dev/null
  docker image inspect "$image" >/dev/null || fail "archive does not contain $image"
  [[ "$(docker image inspect "$image" --format '{{ index .Config.Labels "happyvalley.commit" }}')" == "$commit" ]] || fail "image label happyvalley.commit does not match $commit"

  if docker image inspect "$prod_image" >/dev/null 2>&1; then
    previous_id="$(docker image inspect "$prod_image" --format '{{.Id}}')"
    previous_commit="$(docker image inspect "$prod_image" --format '{{ index .Config.Labels "happyvalley.commit" }}')"
    [[ "$previous_commit" =~ ^[0-9a-f]{7,64}$ ]] || previous_commit="$(date -u +%Y%m%d%H%M%S)"
    rollback_tag="happyvalley-${service}:rollback-${previous_commit}"
    docker tag "$previous_id" "$rollback_tag"
  fi

  docker tag "$image" "$prod_image"
  if ! docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$service"; then
    detail="compose recreation failed"
  elif ! wait_for_service "$service"; then
    detail="service readiness timed out"
  elif ! check_service_health "$service"; then
    detail="health check failed"
  else
    write_record "$service" "$commit" success "$previous_commit" "$detail"
    cleanup_old_images "$service"
    find "$INCOMING_DIR" -maxdepth 1 -type f -name "${service}-${commit}.tar*" -delete
    echo "released $service at $commit"
    return 0
  fi

  if [[ -n "$previous_id" ]]; then
    docker tag "$previous_id" "$prod_image"
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$service" || true
    write_record "$service" "$commit" rolled_back "$previous_commit" "$detail"
  else
    write_record "$service" "$commit" failed "" "$detail"
  fi
  fail "$service deployment failed: $detail"
}

main() {
  [[ $# -eq 3 ]] || fail "usage: $0 <frontend|backend|all> <commit> <image-archive>"
  local service="$1" commit="$2" archive="$3"
  validate_commit "$commit"
  [[ -f "$COMPOSE_FILE" ]] || fail "compose file not found: $COMPOSE_FILE"
  mkdir -p "$INCOMING_DIR" "$RELEASES_DIR" "$MEDIA_DIR"

  case "$service" in
    frontend|backend) release_one "$service" "$commit" "$archive" ;;
    all)
      release_one backend "$commit" "$archive"
      release_one frontend "$commit" "$archive"
      ;;
    *) fail "service must be frontend, backend, or all" ;;
  esac
}

main "$@"
