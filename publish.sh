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
EIA_RUNTIME="$LIVE_REPO/runtime_eia_demo"
PA_RUNTIME="$LIVE_REPO/runtime_pa_demo"
# Wheel demo-deploy legs (MES/MGC/MNQCONT), added 2026-10-15 -- same additive dashboard_extras
# convention as every leg above (see cached-soaring-aho.md). Paper/commissioning runtime dirs,
# not "_demo" like the others, matching their own deploy scripts' naming.
MES_RUNTIME="$LIVE_REPO/runtime_mes_paper"
MGC_RUNTIME="$LIVE_REPO/runtime_mgc_paper"
MNQCONT_RUNTIME="$LIVE_REPO/runtime_mnqcont_paper"
MGC10_RUNTIME="$LIVE_REPO/runtime_mgc10_paper"
MES2R_RUNTIME="$LIVE_REPO/runtime_mes2r_paper"
M6E_RUNTIME="$LIVE_REPO/runtime_m6e_paper"
# Wheel v1.0's own real execution legs (MNQ+MGC buy-and-hold, D2 ladder -> permanent C30-B
# derisk at $500k), added 2026-10-27. Separate from MES/MGC/MNQCONT/MGC10/MES2R above -- those
# are the OLDER, retired-from-"the wheel" P2-governed legs. See
# ~/.claude/plans/groovy-watching-dewdrop.md.
WHEEL_V1_MNQ_RUNTIME="$LIVE_REPO/runtime_wheel_v1_mnq"
WHEEL_V1_MGC_RUNTIME="$LIVE_REPO/runtime_wheel_v1_mgc"
WHEEL_V1_SHADOW_RUNTIME="$LIVE_REPO/runtime_wheel_v1_shadow"
# Ops health control plane (scripts/ops_health_check.py, 2026-10-19) -- read-only pass-through
# of its own fleet_status.json, same additive convention as projection/horizon_risk below.
OPS_HEALTH_JSON="$LIVE_REPO/runtime_ops/fleet_status.json"
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
# EIA petroleum sleeve (HO x1 + RB x1, ONE sleeve; 2026-09-24): per-product status/trades files inside one runtime dir; same additive convention.
EXTRA_EIA_ARGS=()
if [ -f "$EIA_RUNTIME/live_status_ho.json" ] || [ -f "$EIA_RUNTIME/live_status_rb.json" ]; then
  EXTRA_EIA_ARGS=(--eia-dir "$EIA_RUNTIME")
fi
# PA (palladium) leg -- added 2026-10-05, same additive convention as 6J/LE above.
EXTRA_PA_ARGS=()
if [ -f "$PA_RUNTIME/live_status.json" ]; then
  EXTRA_PA_ARGS=(--pa-status "$PA_RUNTIME/live_status.json" --pa-trades "$PA_RUNTIME/pa_trades.jsonl")
fi
# Wheel demo-deploy legs -- same additive convention, same schema-safety guarantee (absent
# entirely if a runtime dir is missing or stale, cannot regress the existing dashboard).
EXTRA_MES_ARGS=()
if [ -f "$MES_RUNTIME/live_status.json" ]; then
  EXTRA_MES_ARGS=(--mes-status "$MES_RUNTIME/live_status.json" --mes-trades "$MES_RUNTIME/mes_trades.jsonl")
fi
EXTRA_MGC_ARGS=()
if [ -f "$MGC_RUNTIME/live_status.json" ]; then
  EXTRA_MGC_ARGS=(--mgc-status "$MGC_RUNTIME/live_status.json" --mgc-trades "$MGC_RUNTIME/mgc_trades.jsonl")
fi
EXTRA_MNQCONT_ARGS=()
if [ -f "$MNQCONT_RUNTIME/live_status.json" ]; then
  EXTRA_MNQCONT_ARGS=(--mnqcont-status "$MNQCONT_RUNTIME/live_status.json" --mnqcont-trades "$MNQCONT_RUNTIME/mnqcont_trades.jsonl")
fi
# Tail sleeves -- added 2026-10-15, same additive convention.
EXTRA_MGC10_ARGS=()
if [ -f "$MGC10_RUNTIME/live_status.json" ]; then
  EXTRA_MGC10_ARGS=(--mgc10-status "$MGC10_RUNTIME/live_status.json" --mgc10-trades "$MGC10_RUNTIME/mgc10_trades.jsonl")
fi
EXTRA_MES2R_ARGS=()
if [ -f "$MES2R_RUNTIME/live_status.json" ]; then
  EXTRA_MES2R_ARGS=(--mes2r-status "$MES2R_RUNTIME/live_status.json" --mes2r-trades "$MES2R_RUNTIME/mes2r_trades.jsonl")
fi
# M6E leg -- RETIRED 2026-10-27 (user: "get rid of M6E too, we are no longer using it"). Its
# process is stopped and its real demo position closed; intentionally left unconditionally empty
# (not wired to $M6E_RUNTIME/live_status.json anymore) so it never resurfaces in the export even
# though that status file still exists on disk, now marked RETIRED.
EXTRA_M6E_ARGS=()
EIA_HO_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$EIA_RUNTIME/live_status_ho.json')).get('order_qty',0))" 2>/dev/null || echo 0)
EIA_RB_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$EIA_RUNTIME/live_status_rb.json')).get('order_qty',0))" 2>/dev/null || echo 0)
LE_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$LE_RUNTIME/live_status.json')).get('order_qty',0))" 2>/dev/null || echo 0)

PROJECTION_JSON="$T1_RUNTIME/portfolio_projection.json"
# Real current 6-leg Wheel (T1 + MNQCONT + MES 1R + MGC 1R + MGC10 + MES2R), real current
# sizing -- supersedes the old T1/6J/LE/ES-shadow projection (2026-10-15; 6J/LE disabled, ES
# is shadow-only, EIA dropped at explicit request -- none belong in this projection anymore).
# T1 and CONT share ONE qty (mutually exclusive legs, same P2 ladder level); MES/MGC are NOT
# P2-governed (not part of today's ladder research), sized at whatever's actually running.
T1_CONT_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$T1_RUNTIME/live_status.json')).get('order_qty',5))" 2>/dev/null || echo 5)
MES_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$MES_RUNTIME/live_status.json')).get('order_qty',1))" 2>/dev/null || echo 1)
MGC_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$MGC_RUNTIME/live_status.json')).get('order_qty',1))" 2>/dev/null || echo 1)
MGC10_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$MGC10_RUNTIME/live_status.json')).get('order_qty',1))" 2>/dev/null || echo 1)
MES2R_QTY=$("$LIVE_REPO/.venv/bin/python3" -c "import json; print(json.load(open('$MES2R_RUNTIME/live_status.json')).get('order_qty',1))" 2>/dev/null || echo 1)
PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/project_wheel_current.py" \
  --t1-cont-qty "$T1_CONT_QTY" --mes-qty "$MES_QTY" --mgc-qty "$MGC_QTY" \
  --mgc10-qty "$MGC10_QTY" --mes2r-qty "$MES2R_QTY" --output "$PROJECTION_JSON" \
  >/dev/null 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) projection calc failed (non-fatal)"
EXTRA_PROJECTION_ARGS=()
if [ -f "$PROJECTION_JSON" ]; then
  EXTRA_PROJECTION_ARGS=(--projection "$PROJECTION_JSON")
fi

# Horizon-matched risk curve -- Wheel v1.0 native (scripts/project_wheel_v1_horizon_risk.py),
# ~200-path block-bootstrap, ~25s of CPU on this single-core box. Replaces the retired 6-leg
# P2-governed project_wheel_horizon_risk.py for THIS panel only (that script is not deleted,
# just no longer invoked here -- see ~/.claude/plans/groovy-watching-dewdrop.md). Only
# recomputed once/day (cache older than 20h, matched to this script's 15-min cron cadence but far
# enough apart not to compete with the live bots every tick).
HORIZON_RISK_JSON="$T1_RUNTIME/horizon_risk.json"
if [ ! -f "$HORIZON_RISK_JSON" ] || [ "$(find "$HORIZON_RISK_JSON" -mmin +1200 2>/dev/null)" ]; then
  PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/project_wheel_v1_horizon_risk.py" \
    "$HORIZON_RISK_JSON" >/dev/null 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) horizon risk calc failed (non-fatal)"
fi
EXTRA_HORIZON_RISK_ARGS=()
if [ -f "$HORIZON_RISK_JSON" ]; then
  EXTRA_HORIZON_RISK_ARGS=(--horizon-risk "$HORIZON_RISK_JSON")
fi
EXTRA_OPS_HEALTH_ARGS=()
if [ -f "$OPS_HEALTH_JSON" ]; then
  EXTRA_OPS_HEALTH_ARGS=(--ops-health "$OPS_HEALTH_JSON")
fi
EXTRA_WHEEL_V1_ARGS=()
if [ -f "$WHEEL_V1_MNQ_RUNTIME/live_status.json" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-mnq-status "$WHEEL_V1_MNQ_RUNTIME/live_status.json")
fi
if [ -f "$WHEEL_V1_MGC_RUNTIME/live_status.json" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-mgc-status "$WHEEL_V1_MGC_RUNTIME/live_status.json")
fi
if [ -f "$WHEEL_V1_SHADOW_RUNTIME/wheel_v1_shadow_status.json" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-shadow-status "$WHEEL_V1_SHADOW_RUNTIME/wheel_v1_shadow_status.json")
fi
# Unrealized P&L marks -- reuses the ALREADY-streaming MNQCONT/MGC bar logs (no new Databento
# connection), same data source wheel_v1_shadow_engine.load_full_calendar() reads.
if [ -f "$MNQCONT_RUNTIME/mnqcont_bars.jsonl" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-mnq-bars "$MNQCONT_RUNTIME/mnqcont_bars.jsonl")
fi
if [ -f "$MGC_RUNTIME/mgc_bars.jsonl" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-mgc-bars "$MGC_RUNTIME/mgc_bars.jsonl")
fi
# Equity -> sizing ladder (ACTIVE T1=70% vs. LEGACY T1=30%) -- a single pickle read, cheap enough
# to recompute every cycle, no mtime cache needed (unlike the horizon-risk Monte Carlo above).
EQUITY_LADDER_JSON="$WHEEL_V1_SHADOW_RUNTIME/equity_ladder.json"
if [ -f "$WHEEL_V1_SHADOW_RUNTIME/wheel_v1_shadow_state.pkl" ]; then
  PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" "$LIVE_REPO/scripts/wheel_v1_equity_ladder.py" \
    "$EQUITY_LADDER_JSON" >/dev/null 2>&1 || echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) equity ladder calc failed (non-fatal)"
fi
if [ -f "$EQUITY_LADDER_JSON" ]; then
  EXTRA_WHEEL_V1_ARGS+=(--wheel-v1-equity-ladder "$EQUITY_LADDER_JSON")
fi

PYTHONPATH="$LIVE_REPO/src" "$LIVE_REPO/.venv/bin/python3" -m mnq_rt1_live.export_t1_private_snapshot \
  --trades "$T1_RUNTIME/t1_trades.jsonl" \
  --status "$T1_RUNTIME/live_status.json" \
  --output "$PRIVATE_SNAPSHOT" \
  --mode "$RUNTIME_MODE" \
  "${EXTRA_6J_ARGS[@]}" \
  "${EXTRA_ON001_ARGS[@]}" \
  "${EXTRA_LE_ARGS[@]}" \
  "${EXTRA_EIA_ARGS[@]}" \
  "${EXTRA_PA_ARGS[@]}" \
  "${EXTRA_MES_ARGS[@]}" \
  "${EXTRA_MGC_ARGS[@]}" \
  "${EXTRA_MNQCONT_ARGS[@]}" \
  "${EXTRA_MGC10_ARGS[@]}" \
  "${EXTRA_MES2R_ARGS[@]}" \
  "${EXTRA_M6E_ARGS[@]}" \
  "${EXTRA_PROJECTION_ARGS[@]}" \
  "${EXTRA_HORIZON_RISK_ARGS[@]}" \
  "${EXTRA_OPS_HEALTH_ARGS[@]}" \
  "${EXTRA_WHEEL_V1_ARGS[@]}"

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
