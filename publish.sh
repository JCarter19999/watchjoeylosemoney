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
# 2026-09-10: REPOINTED from DV_SIGNAL_V1 to FAST_EXPANDED_V1_VALL_D90_1M
# -- same frozen D_t Ridge model, but no V_t entry gate (VALL: trades
# every D_t-extreme decision, any volatility state; V_t still computed
# for telemetry/cohort-labeling only). Deployed in a SEPARATE worktree
# (market_structure_ml-fast-expanded, not market_structure_ml-live) --
# LIVE_REPO above changed accordingly, not just the runtime subdir.
# DV_SIGNAL_V1 itself was stopped the same night (flat, watchdog cron
# paused not deleted); its final snapshot archived at
# watchjoeylosemoney/archive/public_snapshot_DV_SIGNAL_V1_final_2026-09-10.json
# before this repoint (same convention as every prior repoint above).
# Own exporter: export_fast_expanded_v1_private_snapshot.py. RUNTIME_MODE
# read dynamically from the live process's own live_status.json, same
# stale-hardcoded-value lesson as the DV_SIGNAL_V1 repoint above.
#
# 2026-09-20: REPOINTED from FAST_EXPANDED_V1_VALL_D90_1M (stopped 2026-09-11, causal-entry-lookahead finding) to T1
# (canonical 09:33 entry -> RTH-close exit, Tradovate DEMO, 5 MNQ). Own exporter: export_t1_private_snapshot.py in the
# market_structure_ml-live worktree (branch t1-live-demo), same private-snapshot shape so the deployed schema/sanitizer/
# app stay UNCHANGED (no deploy-gap risk). Only real DEMO_EXEC trades from runtime_t1_demo/t1_trades.jsonl are recorded.
# FAST_EXPANDED's final snapshot archived at archive/public_snapshot_FAST_EXPANDED_V1_final_2026-09-20.json first.
#
# 2026-09-22: T1 now runs FUSED with a second, independent instrument (run_t1_fused_live.py: 5 MNQ C3, unchanged,
# plus 2x 6J.v.0 HOLD -- a London-open opening-burst analogue of the same rule; T1_6J_LONDON_C3_PORT_V1 found the
# exit/re-entry controller doesn't transfer to 6J, so it's HOLD-only). The MNQ leg's export path above is completely
# unchanged (still the only thing that fills the core schema); the 6J leg is added ONLY as an optional
# dashboard_extras['t1_6j_leg'] panel (see export_t1_private_snapshot.py's --t1-6j-status/--t1-6j-trades and
# streamlit_app.py's render_6j_leg) -- same additive, schema-safe convention as t1_shadow_controllers, so this
# cannot regress the existing MNQ dashboard even if the 6J runtime dir is ever missing or stale.
T1_RUNTIME="$LIVE_REPO/runtime_t1_demo"
T1_6J_RUNTIME="$LIVE_REPO/runtime_t1_6j_demo"
ON001_RUNTIME="$LIVE_REPO/runtime_on001_demo"
LE_RUNTIME="$LIVE_REPO/runtime_le_demo"
PRIVATE_SNAPSHOT="$LIVE_REPO/runtime_t1_demo/private_snapshot.json"
LOCK=/tmp/watchjoeylosemoney-publish.lock

exec 9>"$LOCK"
flock -n 9 || exit 0

cd "$LIVE_REPO"

RUNTIME_MODE=$("$LIVE_REPO/.venv/bin/python3" -c "
import json
# T1's live_status.json mode vocabulary is {OBSERVE, DEMO_EXEC}; this site's is {SHADOW, DEMO, LIVE}.
mode = 'SHADOW'
try:
    mode = json.load(open('$T1_RUNTIME/live_status.json')).get('mode', 'OBSERVE')
except (FileNotFoundError, json.JSONDecodeError):
    pass
print({'DEMO_EXEC': 'DEMO', 'OBSERVE': 'SHADOW'}.get(mode, 'SHADOW'))
")

# 2026-09-21: refresh the offline SHADOW-controller tracker (C2/C3 vs HOLD; research telemetry only, read-only on the live
# runtime files, writes only t1_shadow_controllers.jsonl) so the exporter can publish aggregate points into the pass-through
# dashboard_extras. Non-fatal: a tracker failure must never block the normal publish. Low priority (single-core box).
PYTHONPATH="$LIVE_REPO/src" nice -n 15 "$LIVE_REPO/.venv/bin/python3" -m scripts.t1_shadow_controllers \
  --runtime "$T1_RUNTIME" >/dev/null 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) shadow tracker failed (non-fatal)"

EXTRA_6J_ARGS=()
if [ -f "$T1_6J_RUNTIME/live_status.json" ]; then
  EXTRA_6J_ARGS=(--t1-6j-status "$T1_6J_RUNTIME/live_status.json" --t1-6j-trades "$T1_6J_RUNTIME/t1_6j_trades.jsonl")
fi

# ON-001/ES leg, same additive dashboard_extras['on001_leg'] convention as 6J above (2026-09-24: "yes, i want
# it to show up on the published ledger"). Same schema-safety guarantee: absent entirely if this runtime dir
# is ever missing or stale, cannot regress the existing MNQ/6J dashboard.
EXTRA_ON001_ARGS=()
if [ -f "$ON001_RUNTIME/live_status.json" ]; then
  EXTRA_ON001_ARGS=(--on001-status "$ON001_RUNTIME/live_status.json" --on001-trades "$ON001_RUNTIME/on001_trades.jsonl")
fi

# Projected annualized P&L + historical-window MDD at whatever contract sizing is CURRENTLY live (2026-09-24:
# "I also want a project annualized + projected max drawdown section") -- reads live order_qty fresh off each
# leg's status file every cycle, so it always reflects a same-day resize. Non-fatal: a failure here must never
# block the normal publish (same convention as the shadow-controller tracker above).
# LE (Live Cattle) leg -- FUNDED third leg as of 2026-09-24 (replaced ES): same additive convention as 6J/ES above.
EXTRA_LE_ARGS=()
if [ -f "$LE_RUNTIME/live_status.json" ]; then
  EXTRA_LE_ARGS=(--le-status "$LE_RUNTIME/live_status.json" --le-trades "$LE_RUNTIME/le_trades.jsonl")
fi
LE_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$LE_RUNTIME/live_status.json')).get('order_qty',0))" 2>/dev/null || echo 0)

PROJECTION_JSON="$T1_RUNTIME/portfolio_projection.json"
T1_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$T1_RUNTIME/live_status.json')).get('order_qty',5))" 2>/dev/null || echo 5)
SIXJ_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$T1_6J_RUNTIME/live_status.json')).get('order_qty',4))" 2>/dev/null || echo 4)
ES_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$ON001_RUNTIME/live_status.json')).get('order_qty',3))" 2>/dev/null || echo 3)
PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/project_annualized_mdd.py" \
  --t1-qty "$T1_QTY" --sixj-qty "$SIXJ_QTY" --es-qty "$ES_QTY" --le-qty "$LE_QTY" --output "$PROJECTION_JSON" \
  >/dev/null 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) projection calc failed (non-fatal)"
EXTRA_PROJECTION_ARGS=()
if [ -f "$PROJECTION_JSON" ]; then
  EXTRA_PROJECTION_ARGS=(--projection "$PROJECTION_JSON")
fi

PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" -m mnq_rt1_live.export_t1_private_snapshot \
  --trades "$T1_RUNTIME/t1_trades.jsonl" \
  --status "$T1_RUNTIME/live_status.json" \
  --output "$PRIVATE_SNAPSHOT" \
  --mode "$RUNTIME_MODE" \
  "${EXTRA_6J_ARGS[@]}" \
  "${EXTRA_ON001_ARGS[@]}" \
  "${EXTRA_LE_ARGS[@]}" \
  "${EXTRA_PROJECTION_ARGS[@]}"

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
