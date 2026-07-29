#!/usr/bin/env bash
set -euo pipefail

ROOT_SCRIPTS="/usr/local/lib/happyvalley-deploy"
DEPLOY_ROOT="/root/happyvalley-deploy"
INCOMING_DIR="$DEPLOY_ROOT/incoming"
fail() { echo "deployment access denied: $*" >&2; exit 126; }

command="${1:-${SSH_ORIGINAL_COMMAND:-}}"
read -r action service commit extra <<<"$command"
[[ -n "$action" && -z "${extra:-}" ]] || fail "invalid command"
[[ "$service" == frontend || "$service" == backend ]] || fail "invalid service"
[[ "$commit" =~ ^[0-9a-f]{7,64}$ ]] || fail "invalid commit"

case "$action" in
  receive-image)
    mkdir -p "$INCOMING_DIR"
    umask 077
    cat > "$INCOMING_DIR/${service}-${commit}.tar.gz"
    ;;
  release-image)
    exec "$ROOT_SCRIPTS/release-image.sh" "$service" "$commit" "$INCOMING_DIR/${service}-${commit}.tar.gz"
    ;;
  rollback)
    exec "$ROOT_SCRIPTS/rollback.sh" "$service" "$commit"
    ;;
  *) fail "unsupported command" ;;
esac
