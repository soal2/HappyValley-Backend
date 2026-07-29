#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ $# -eq 1 ]] || { echo "usage: $0 <deploy-public-key-file>" >&2; exit 1; }

key_file="$1"
[[ -f "$key_file" ]] || { echo "public key file not found: $key_file" >&2; exit 1; }
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root_scripts="/usr/local/lib/happyvalley-deploy"
deploy_root="/root/happyvalley-deploy"

install -d -m 0755 "$root_scripts" "$deploy_root/incoming" "$deploy_root/releases" "$deploy_root/data/media"
install -m 0755 "$script_dir/release-image.sh" "$root_scripts/release-image.sh"
install -m 0755 "$script_dir/rollback.sh" "$root_scripts/rollback.sh"
install -m 0755 "$script_dir/ssh-command-dispatch.sh" "$root_scripts/ssh-command-dispatch.sh"
install -m 0644 "$script_dir/docker-compose.prod.yml" "$deploy_root/deploy/docker-compose.prod.yml"

id deploy >/dev/null 2>&1 || useradd --create-home --shell /bin/bash deploy
usermod --shell /bin/bash deploy
install -d -m 0700 -o deploy -g deploy /home/deploy/.ssh
printf 'deploy ALL=(root) NOPASSWD: /usr/local/lib/happyvalley-deploy/ssh-command-dispatch.sh\n' > /etc/sudoers.d/happyvalley-deploy
chmod 0440 /etc/sudoers.d/happyvalley-deploy
visudo -cf /etc/sudoers.d/happyvalley-deploy
printf '%s %s\n' 'command="/usr/bin/sudo /usr/local/lib/happyvalley-deploy/ssh-command-dispatch.sh \"$SSH_ORIGINAL_COMMAND\"",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty' "$(< "$key_file")" > /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 0600 /home/deploy/.ssh/authorized_keys

echo "server deployment base installed; add the matching private key to GitHub secret DEPLOY_SSH_PRIVATE_KEY"
