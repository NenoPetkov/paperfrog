#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
export PATH=/usr/local/bin:/usr/bin:/bin
root=/var/www/paperfrog
expected=${1:?Expected commit is required}
policy=${2:-preserve}
[[ $policy == preserve || $policy == strict-auto || $policy == momentum || $policy == combined ]] || exit 2
if [[ $policy == momentum || $policy == combined ]]; then
  profile=${3:?Momentum profile is required}
  hold=${4:?Holding hours are required}
  [[ $profile =~ ^momentum(_selective|_breakout|_pullback)?$ && $hold =~ ^(1|2|4|8)$ ]] || exit 2
fi
[[ $expected =~ ^[0-9a-f]{40}$ ]] || exit 2
cd "$root"
exec 9>/run/lock/paperfrog-deploy.lock
flock -n 9 || { echo 'Another server deploy is in progress.' >&2; exit 1; }
[[ -z $(git status --porcelain) ]] || { echo 'Server checkout has local changes; refusing to overwrite them.' >&2; exit 1; }
[[ $(git branch --show-current) == master ]] || exit 1
git fetch origin master
[[ $(git rev-parse origin/master) == "$expected" ]] || { echo 'Remote master differs from the requested commit.' >&2; exit 1; }
git merge-base --is-ancestor HEAD "$expected" || { echo 'Deployment must be a fast-forward.' >&2; exit 1; }

candidate=$(mktemp -d /var/tmp/paperfrog-check.XXXXXXXX)
context=$(mktemp /var/tmp/paperfrog-context.XXXXXXXX)
stopped=0
started=0
paper_active=0
if systemctl is-active --quiet paperfrog-momentum-paper.service; then
  paper_active=1
fi
cleanup() {
  result=$?
  trap - EXIT
  git -C "$root" worktree remove --force "$candidate" >/dev/null 2>&1 || true
  rm -f "$context"
  if (( result != 0 )); then
    if (( stopped && ! started )); then
      echo 'Deploy failed after shutdown. The bot remains stopped; its ledger and hosted stops are retained. Inspect the error before restarting.' >&2
    elif (( started )); then
      echo 'Startup verification failed. Inspect paperfrog.service; no database rollback or risk-lock reset was attempted.' >&2
    else
      echo 'Release checks failed; the running bot was not stopped.' >&2
    fi
  fi
  exit "$result"
}
trap cleanup EXIT

.venv/bin/python scripts/server_ops.py context --require-arm > "$context"
git worktree add --detach "$candidate" "$expected"
(
  cd "$candidate"
  uv sync --frozen --python 3.12.14
  uv run --no-sync pytest -q -m 'not testnet'
  uv run --no-sync ruff check src tests scripts
  uv run --no-sync mypy --strict
  uv run --no-sync mypy --strict src/paperfrog/simple/carry.py src/paperfrog/simple/live_broker.py
  bash -n depoy.sh scripts/deploy_remote.sh
)
"$candidate/.venv/bin/python" "$candidate/scripts/server_ops.py" --root "$root" compare "$context"
systemd-analyze verify "$candidate/deploy/paperfrog.service" "$candidate/deploy/paperfrog-daily-review.service" "$candidate/deploy/paperfrog-daily-review.timer"
.venv/bin/python scripts/server_ops.py space

echo 'Release verified. Stopping writers and taking a consistent state backup.'
systemctl stop paperfrog-daily-review.timer paperfrog-daily-review.service
systemctl stop paperfrog.service
if (( paper_active )); then
  systemctl stop paperfrog-momentum-paper.service
fi
stopped=1
.venv/bin/python scripts/server_ops.py idle
# Detect configuration/account changes that occurred during the release checks.
.venv/bin/python scripts/server_ops.py compare "$context"
backup=/var/backups/paperfrog/deploy-$(date -u +%Y%m%dT%H%M%SZ)-${expected:0:12}
.venv/bin/python scripts/server_ops.py backup "$backup"
git pull --ff-only origin master
[[ $(git rev-parse HEAD) == "$expected" ]] || { echo 'Master changed during deployment; leaving the bot stopped.' >&2; exit 1; }
uv sync --frozen --python 3.12.14
.venv/bin/python scripts/server_ops.py compare "$context"
if [[ $policy == strict-auto ]]; then
  .venv/bin/python scripts/server_ops.py strategy-strict
elif [[ $policy == momentum || $policy == combined ]]; then
  carry_args=()
  if [[ $policy == combined ]]; then carry_args=(--with-carry); fi
  .venv/bin/python scripts/server_ops.py activate-momentum "$context" --profile "$profile" --hold-hours "$hold" --confirm 'ACTIVATE LIVE MOMENTUM' "${carry_args[@]}"
fi
install -m 644 deploy/paperfrog.service deploy/paperfrog-daily-review.service deploy/paperfrog-daily-review.timer /etc/systemd/system/
systemctl daemon-reload
.venv/bin/python scripts/server_ops.py rearm "$context"
systemctl enable paperfrog.service paperfrog-daily-review.timer
systemctl reset-failed paperfrog.service
systemctl start paperfrog.service
started=1
.venv/bin/python scripts/server_ops.py wait --timeout 240
systemctl start paperfrog-daily-review.timer
if [[ $policy == momentum || $policy == combined ]]; then
  if systemctl cat paperfrog-momentum-paper.service >/dev/null 2>&1; then
    systemctl disable paperfrog-momentum-paper.service
  fi
elif (( paper_active )); then
  systemctl start paperfrog-momentum-paper.service
fi
echo "Deploy complete: $expected; state backup: $backup"
