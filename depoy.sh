#!/usr/bin/env bash
# Run locally after committing changes. The server owns all runtime data after migration.
set -Eeuo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
host=${PAPERFROG_DEPLOY_HOST:-root@176.31.163.20}
ssh_options=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

case ${1:-} in
  --help|-h)
    echo 'Usage: ./depoy.sh [--status]'
    echo 'Commits must already exist. Pushes master, checks the release, backs up state, restarts and rearms an already-authorized LIVE bot.'
    exit 0 ;;
  --status)
    exec ssh "${ssh_options[@]}" "$host" 'cd /var/www/paperfrog && .venv/bin/python scripts/server_ops.py status' ;;
  '') ;;
  *) echo 'Unknown option. Use --help.' >&2; exit 2 ;;
esac

exec 9>.git/paperfrog-deploy.lock
flock -n 9 || { echo 'Another local deploy is in progress.' >&2; exit 1; }
[[ $(git branch --show-current) == master ]] || { echo 'Deploy requires the master branch.' >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo 'Commit or stash changes before deploying; nothing was sent.' >&2; exit 1; }
[[ -x .venv/bin/python ]] || { echo 'Local Python environment missing; run uv sync --frozen.' >&2; exit 1; }
.venv/bin/python scripts/server_ops.py idle
commit=$(git rev-parse HEAD)
git push origin master
echo "Deploying ${commit:0:12} to $host"
# No .env, databases, data or reports are uploaded by routine deployment.
ssh "${ssh_options[@]}" "$host" "bash -s -- $commit" < scripts/deploy_remote.sh
