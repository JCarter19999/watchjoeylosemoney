#!/usr/bin/env bash
# Publisher: private trade_journal.jsonl -> private_snapshot.json ->
# (whitelist-only) sanitizer.py -> public_snapshot.json -> git push.
# Trading never waits on this; this script has no ability to touch the
# guardian, the ledger's halted state, or submit an order -- read-only
# on the trading side, push-only on the website side.
set -euo pipefail

LIVE_REPO=/home/joey/market_structure_ml-live/live
WEB_REPO=/home/joey/watchjoeylosemoney
PRIVATE_SNAPSHOT="$LIVE_REPO/runtime/private_snapshot.json"
LOCK=/tmp/watchjoeylosemoney-publish.lock

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$LIVE_REPO"
"$LIVE_REPO/.venv/bin/python3" -m mnq_rt1_live.export_private_snapshot --output "$PRIVATE_SNAPSHOT"

cd "$WEB_REPO"
"$WEB_REPO/.venv/bin/python3" sanitizer.py \
  --input "$PRIVATE_SNAPSHOT" \
  --output public_snapshot.json \
  --schema public_snapshot.schema.json \
  --live-delay-minutes 15

"$WEB_REPO/.venv/bin/pytest" -q

git add public_snapshot.json
if git diff --cached --quiet; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) no change, skipping push"
  exit 0
fi

git -c user.name="wjlm-publisher" -c user.email="wjlm-publisher@users.noreply.github.com" \
  commit -q -m "data: publish sanitized snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push -q origin main
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) published"
