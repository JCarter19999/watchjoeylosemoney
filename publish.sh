#!/usr/bin/env bash
# Publisher: private trade_journal.jsonl -> private_snapshot.json ->
# (whitelist-only) sanitizer.py -> public_snapshot.json -> git push.
# Trading never waits on this; this script has no ability to touch the
# guardian, the ledger's halted state, or submit an order -- read-only
# on the trading side, push-only on the website side.
set -euo pipefail

LIVE_REPO=/home/joey/market_structure_ml-live/live
WEB_REPO=/home/joey/watchjoeylosemoney
# 2026-08-18: RT1-V2-D20 commissioning -- old V1 production runtime/
# archived (real execution stopped), V2 (D=20s debounce) now runs in
# runtime_v2_d20_paper/ with real Tradovate DEMO fills. This site's
# schema/sanitizer only understand a single real-execution feed (no
# concept of the new zero-execution V1 shadow watcher -- its rows would
# be filtered out by _is_real_closed_trade() even if pointed at it
# anyway), so this repoints at V2 paper rather than adding a second feed
# here. See live/HANDOFF.md's 2026-08-18 section for the V1-shadow-vs-V2
# comparison, which lives on the private btc-dashboard instead.
#
# 2026-08-18/19: was TEMPORARILY repointed at runtime_live_slippage_v1/
# for a user-authorized, capped 5-trade REAL-MONEY commissioning run
# (TRADOVATE_ENV=live) -- completed (hit the 5-trade cap, one bracket-
# rejection/tick-grid incident fixed mid-run, see live/HANDOFF.md's
# 2026-08-19 section for the full incident + slippage writeup). Reverted
# back to the paused demo bot's runtime now that it's resumed.
#
# 2026-08-19: export_private_snapshot.py's --mode used to be hardcoded
# "DEMO" with no way to override it -- 5 real-money trades from the live
# run above got published to the public dashboard labeled "DEMO" during
# the window this pointed at runtime_live_slippage_v1/. Fixed at the
# source: --mode is now required and must match whichever runtime dir
# V2_RUNTIME actually points at. If this ever gets repointed at a live
# runtime again, change BOTH V2_RUNTIME and --mode below together.
#
# 2026-09-08: REPOINTED from RT1 (runtime_v2_d20_paper, permanently
# stopped) to G1. G1 itself was formally ARCHIVED 2026-09-09 (falsified
# under causal reconstruction -- see g1_postmortem_ARCHIVED_2026_09_09
# project memory); G1's final snapshot was archived at
# watchjoeylosemoney/archive/public_snapshot_G1_final_2026-09-10.json
# before this repoint (same convention as RT1's archive above).
#
# 2026-09-10: REPOINTED from G1 to DV_SIGNAL_V1 -- the frozen D_t/V_t
# HIGH_V-interaction signal, currently deployed SHADOW-ONLY (no real or
# even demo order submission; execution_enabled is always false for this
# strategy right now). Has its own exporter
# (export_dv_signal_private_snapshot.py) producing the same private-
# snapshot shape, mode="SHADOW" (a schema-native value, not an
# extension). Paper size is 5 MNQ-equivalent (DV_SIGNAL_SHADOW_QTY, a
# pure quantity multiplier on the frozen decision, never a rule change)
# -- see dv_signal_live_service.py's shadow_qty for the full rationale.
DV_SIGNAL_RUNTIME="$LIVE_REPO/runtime_dv_signal_shadow"
RUNTIME_MODE="SHADOW"
PRIVATE_SNAPSHOT="$LIVE_REPO/runtime/private_snapshot.json"
LOCK=/tmp/watchjoeylosemoney-publish.lock

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$LIVE_REPO"

"$LIVE_REPO/.venv/bin/python3" -m mnq_rt1_live.export_dv_signal_private_snapshot \
  --decisions "$DV_SIGNAL_RUNTIME/dv_signal_decisions.jsonl" \
  --trades "$DV_SIGNAL_RUNTIME/dv_signal_trades.jsonl" \
  --status "$DV_SIGNAL_RUNTIME/live_status.json" \
  --output "$PRIVATE_SNAPSHOT" \
  --mode "$RUNTIME_MODE"

cd "$WEB_REPO"
"$WEB_REPO/.venv/bin/python3" sanitizer.py \
  --input "$PRIVATE_SNAPSHOT" \
  --output public_snapshot.json \
  --schema public_snapshot.schema.json \
  --live-delay-minutes 15

# pytest here dropped 2026-08-16: this cron fires every 15min unconditionally
# and pytest is CPU-heavy enough to collide with the live RT1 bot on this
# same single-core box -- exactly the contention pattern already diagnosed
# as the root cause of a prior multi-second order-submission stall (see
# mnq_rt1_live_guardian_instrumentation_gap memory / "Trade 2" incident).
# Re-add only once this box is deployment-only or the bot is off it.

git add public_snapshot.json
if git diff --cached --quiet; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) no change, skipping push"
  exit 0
fi

git -c user.name="wjlm-publisher" -c user.email="wjlm-publisher@users.noreply.github.com" \
  commit -q -m "data: publish sanitized snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git push -q origin main
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) published"
