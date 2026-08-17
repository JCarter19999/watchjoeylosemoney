"""Whitelist-only transform: private_snapshot.json -> public_snapshot.json.

This is the only thing that is allowed to decide what the public website
ever sees. It builds the output object field-by-field from a fixed
whitelist -- it does not copy the private object and delete known-bad
keys, because a blacklist silently leaks any new private field nobody
remembered to exclude.

Trading never waits on this: the trading process's own export step
writes private_snapshot.json independently, and this script (and
whatever fails downstream of it -- GitHub, Streamlit, DNS) has zero
ability to affect Guardian, RT1, or the broker connection.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = "1.0"
MIN_LIVE_DELAY_MINUTES = 10
MAX_LATEST_TRADES = 25

# Thresholds match scripts/analyze_bar_timing.py and the private dashboard's
# RT1 tab in the live repo (2026-08-15 clean-host latency experiment) --
# keep the three in sync if they ever change.
LATENCY_HEALTHY_P95_MS = 500
LATENCY_HEALTHY_P99_MS = 1000


_EMPTY_STAGE = {"n": 0, "median_ms": None, "p95_ms": None, "p99_ms": None}
LATENCY_STAGE_KEYS = (
    "queue_wait_ms", "ingest_and_decision_ms", "get_account_http_ms",
    "submit_order_ms", "poll_until_filled_ms", "post_entry_reconciliation_ms",
    "total_bar_to_submit_ms",
)


def _latency_verdict(latency: dict[str, Any]) -> str:
    n = latency.get("bars_sampled", 0)
    if not n:
        return "INSUFFICIENT_DATA"
    if latency.get("stalls_over_2s", 0) > 0:
        return "NEEDS_UPGRADE"
    # ingest_and_decision_ms (not total_bar_to_submit_ms) gates the overall
    # verdict: it's present for essentially every bar (steady-state
    # per-bar processing cost), where total_bar_to_submit_ms only exists
    # on the rare bars an entry/exit actually opened -- gating on that
    # would report INSUFFICIENT_DATA through long quiet stretches even
    # though thousands of ordinary bars processed fine.
    steady_state = latency.get("stages", {}).get("ingest_and_decision_ms", _EMPTY_STAGE)
    p95 = steady_state.get("p95_ms")
    p99 = steady_state.get("p99_ms")
    if (latency.get("stalls_over_750ms", 0) > 0
            or p95 is None or p95 > LATENCY_HEALTHY_P95_MS
            or p99 is None or p99 > LATENCY_HEALTHY_P99_MS):
        return "MARGINAL"
    return "HEALTHY"


def parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _round(value: float | None, ndigits: int = 4) -> float | None:
    return None if value is None else round(float(value), ndigits)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(pct * (len(s) - 1))))
    return s[idx]


def build_public_snapshot(private: dict[str, Any], now: datetime, live_delay_minutes: int) -> dict[str, Any]:
    mode = private["mode"]
    requires_live_delay = mode == "LIVE"

    if requires_live_delay:
        if live_delay_minutes < MIN_LIVE_DELAY_MINUTES:
            raise ValueError(f"Real-money publication delay must be >= {MIN_LIVE_DELAY_MINUTES} minutes")
        delay = live_delay_minutes
    else:
        delay = 0

    # Once the dataset contains any LIVE-mode closed trade, keep the LIVE
    # delay active even if the process is currently PAUSED/OFFLINE/FAULT/
    # DEMO -- otherwise a mode flip could release a trade that was still
    # supposed to be embargoed.
    ever_live = any(row.get("mode") == "LIVE" for row in private.get("trades", []))
    if ever_live and delay < live_delay_minutes:
        delay = max(delay, live_delay_minutes)

    cutoff = now - timedelta(minutes=delay)

    visible_trades = []
    for row in private.get("trades", []):
        closed_at = row.get("closed_at_utc")
        if not closed_at:
            continue  # never publish an open/incomplete trade
        closed_dt = parse_utc(closed_at)
        if row.get("mode") == "LIVE" and closed_dt > cutoff:
            continue  # still embargoed
        visible_trades.append(row)

    visible_trades.sort(key=lambda r: r["closed_at_utc"])

    starting_equity = float(private.get("display_starting_equity_usd", 0.0))
    equity_curve = []
    running = starting_equity
    peak = starting_equity
    for row in visible_trades:
        running += float(row["pnl_usd"])
        peak = max(peak, running)
        equity_curve.append({
            "ts_utc": row["closed_at_utc"],
            "equity_usd": round(running, 2),
            "cum_pnl_usd": round(running - starting_equity, 2),
            "drawdown_usd": round(max(0.0, peak - running), 2),
        })

    today = now.date().isoformat()
    wins = sum(1 for r in visible_trades if r["pnl_usd"] > 0)
    losses = sum(1 for r in visible_trades if r["pnl_usd"] < 0)
    breakeven = sum(1 for r in visible_trades if r["pnl_usd"] == 0)
    n = len(visible_trades)
    win_rate = (wins / n) if n else None
    gross_profit = sum(r["pnl_usd"] for r in visible_trades if r["pnl_usd"] > 0)
    gross_loss = sum(-r["pnl_usd"] for r in visible_trades if r["pnl_usd"] < 0)
    expectancy = (sum(r["pnl_usd"] for r in visible_trades) / n) if n else None
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if n == 0 else float("inf"))
    if profit_factor == float("inf"):
        profit_factor = None  # undefined (no losses yet) -- null beats a bogus infinity in JSON

    realized_all_time = sum(r["pnl_usd"] for r in visible_trades)
    realized_today = sum(r["pnl_usd"] for r in visible_trades if r["closed_at_utc"].startswith(today))
    max_dd = max((pt["drawdown_usd"] for pt in equity_curve), default=0.0)
    current_dd = equity_curve[-1]["drawdown_usd"] if equity_curve else 0.0

    exec_rows = [r for r in visible_trades if r.get("entry_submit_to_fill_observed_ms") is not None]
    ack_ms = [r["signal_to_order_latency_ms"] for r in exec_rows if r.get("signal_to_order_latency_ms") is not None]
    fill_ms = [r["entry_submit_to_fill_observed_ms"] for r in exec_rows]
    slip_dollars = [
        (r.get("entry_slippage_dollars") or 0.0) + (r.get("exit_slippage_dollars") or 0.0)
        for r in exec_rows
    ]
    # $2.00/point, 0.25-point ticks for MNQ -> $0.50/tick.
    slip_ticks = [d / 0.50 for d in slip_dollars]

    public = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": now.isoformat().replace("+00:00", "Z"),
        "data_as_of_utc": cutoff.isoformat().replace("+00:00", "Z"),
        "publication_delay_minutes": delay,
        "mode": mode,
        "status": {
            "service_state": private["service_state"],
            "guardian_state": private["guardian_state"],
            "market_data_state": private["market_data_state"],
            "execution_enabled": bool(private["execution_enabled"]),
            "data_delayed": delay > 0,
            "message": private["message"],
        },
        "stats": {
            "display_starting_equity_usd": round(starting_equity, 2),
            "display_equity_usd": round(starting_equity + realized_all_time, 2),
            "realized_pnl_today_usd": round(realized_today, 2),
            "realized_pnl_all_time_usd": round(realized_all_time, 2),
            "current_drawdown_usd": round(current_dd, 2),
            "max_drawdown_usd": round(max_dd, 2),
            "closed_trades_today": sum(1 for r in visible_trades if r["closed_at_utc"].startswith(today)),
            "closed_trades_all_time": n,
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "win_rate": _round(win_rate),
            "expectancy_usd": _round(expectancy),
            "profit_factor": _round(profit_factor),
        },
        "execution_telemetry": {
            "sample_size": len(exec_rows),
            "submit_to_ack_ms_median": _round(_percentile(ack_ms, 0.5), 1),
            "submit_to_ack_ms_p95": _round(_percentile(ack_ms, 0.95), 1),
            "submit_to_fill_ms_median": _round(_percentile(fill_ms, 0.5), 1),
            "submit_to_fill_ms_p95": _round(_percentile(fill_ms, 0.95), 1),
            "slippage_ticks_mean": _round(sum(slip_ticks) / len(slip_ticks)) if slip_ticks else None,
            "slippage_ticks_p95": _round(_percentile(slip_ticks, 0.95)),
            "slippage_usd_mean": _round(sum(slip_dollars) / len(slip_dollars)) if slip_dollars else None,
        },
        "latency_health": {
            "bars_sampled": (latency := private.get("latency", {})).get("bars_sampled", 0),
            "stalls_over_750ms": latency.get("stalls_over_750ms", 0),
            "stalls_over_2s": latency.get("stalls_over_2s", 0),
            "cpu_median_pct": _round(latency.get("cpu_median_pct"), 1),
            "load1_median": _round(latency.get("load1_median"), 2),
            "verdict": _latency_verdict(latency),
            "stages": {
                key: {
                    "n": (stage := latency.get("stages", {}).get(key, _EMPTY_STAGE))["n"],
                    "median_ms": stage["median_ms"],
                    "p95_ms": stage["p95_ms"],
                    "p99_ms": stage["p99_ms"],
                }
                for key in LATENCY_STAGE_KEYS
            },
        },
        "warmup_replay": {
            "bars_replayed": private.get("warmup_replay", {}).get("bars_replayed", 0),
            "elapsed_wall_seconds": _round(private.get("warmup_replay", {}).get("elapsed_wall_seconds"), 2),
            "queue_wait_max_ms": _round(private.get("warmup_replay", {}).get("queue_wait_max_ms"), 1),
        },
        "reliability": {
            "restart_count": private.get("reliability", {}).get("restart_count"),
            "first_started_at_utc": private.get("reliability", {}).get("first_started_at_utc"),
            "last_started_at_utc": private.get("reliability", {}).get("last_started_at_utc"),
            "crash_report_count": private.get("reliability", {}).get("crash_report_count", 0),
        },
        "guardian_transitions": [
            {
                "ts_utc": t["ts_utc"],
                "from_state": t["from_state"],
                "to_state": t["to_state"],
                "reason": t["reason"],
            }
            for t in private.get("guardian_transitions", [])
            # Same embargo as trades: a LIVE-mode transition timestamp can
            # reveal real-time position status (e.g. ARMED -> IN_POSITION)
            # ahead of the delay that's supposed to protect it -- gated on
            # the same cutoff, not just left unfiltered because transitions
            # aren't literally a "trade".
            if not (mode == "LIVE" and parse_utc(t["ts_utc"]) > cutoff)
        ][-MAX_LATEST_TRADES:][::-1],
        "equity_curve": equity_curve,
        "latest_trades": [
            {
                "closed_at_utc": r["closed_at_utc"],
                "session_date": r["closed_at_utc"][:10],
                "side": r["side"].upper(),
                "pnl_usd": round(r["pnl_usd"], 2),
                "exit_reason": r["exit_reason"],
                "duration_seconds": r["duration_seconds"],
                "mode": r["mode"],
                "entry_rss_mb": _round(r.get("entry_rss_mb"), 1),
                "entry_process_cpu_pct": _round(r.get("entry_process_cpu_pct"), 1),
                "entry_host_cpu_user_pct": _round(r.get("entry_host_cpu_user_pct"), 1),
                "entry_host_load1": _round(r.get("entry_host_load1"), 2),
                "expected_net_pnl_model_usd": _round(r.get("expected_net_pnl_model")),
            }
            for r in visible_trades[-MAX_LATEST_TRADES:][::-1]
        ],
        "pnl_waterfall": _visible_waterfall(private, mode, cutoff),
    }
    return public


MAX_WATERFALL_TRADES = 25


def _visible_waterfall(private: dict[str, Any], mode: str, cutoff: datetime) -> dict[str, Any]:
    """Backtest -> Live Bridge panel #1 (2026-08-17): Model P&L -> A -> B
    -> C -> Fees -> Realized, per-trade and aggregated. private["pnl_
    waterfall"] is scripts/build_pnl_waterfall.py's output, refreshed on
    its own separate (not-every-15-min) schedule -- may legitimately be
    the empty default if it hasn't run yet or no real trades exist since
    the last refresh.

    Same embargo discipline as `trades`/`guardian_transitions` above: a
    waterfall row carries a real exit timestamp and P&L, so a LIVE-mode
    row past the live-delay cutoff must not be visible yet -- AND the
    published aggregate must be recomputed over only the VISIBLE rows,
    not passed through from the private aggregate (which could otherwise
    leak an embargoed trade's contribution to a mean that's already
    public)."""
    raw = private.get("pnl_waterfall", {})
    all_trades = raw.get("trades", [])
    visible = [
        t for t in all_trades
        if t.get("exit_time") and not (mode == "LIVE" and parse_utc(t["exit_time"]) > cutoff)
    ]
    visible.sort(key=lambda t: t["exit_time"])
    n = len(visible)

    def _mean(key: str) -> float | None:
        return (sum(t[key] for t in visible) / n) if n else None

    return {
        "n_trades": n,
        "n_missing_decomposition": raw.get("n_missing_decomposition", 0),
        "aggregate": {
            "model_expectancy_usd": _round(_mean("model_pnl_dollars")),
            "a_expectancy_usd": _round(_mean("a_semantic_reference_gap_dollars")),
            "b_expectancy_usd": _round(_mean("b_decision_to_submit_dollars")),
            "c_expectancy_usd": _round(_mean("c_execution_residual_dollars")),
            "fees_expectancy_usd": _round(_mean("fees_dollars")),
            "realized_expectancy_usd": _round(_mean("realized_pnl_dollars")),
        },
        "trades": [
            {
                "trade_id": t["trade_id"],
                "closed_at_utc": t["exit_time"],
                "side": t["side"].upper(),
                "exit_reason": t.get("exit_reason") or "UNKNOWN",
                "model_pnl_usd": round(t["model_pnl_dollars"], 2),
                "a_usd": round(t["a_semantic_reference_gap_dollars"], 2),
                "b_usd": round(t["b_decision_to_submit_dollars"], 2),
                "c_usd": round(t["c_execution_residual_dollars"], 2),
                "fees_usd": round(t["fees_dollars"], 2),
                "realized_pnl_usd": round(t["realized_pnl_dollars"], 2),
            }
            for t in visible[-MAX_WATERFALL_TRADES:][::-1]
        ],
    }


def validate_snapshot(snapshot: dict[str, Any], schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(snapshot), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5])
        raise ValueError(details)


def atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--live-delay-minutes", type=int, default=15)
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    snapshot = build_public_snapshot(source, now, args.live_delay_minutes)
    validate_snapshot(snapshot, args.schema)
    atomic_write_json(args.output, snapshot)
    print(f"Published {len(snapshot['latest_trades'])} visible trade(s), mode={snapshot['mode']}, delay={snapshot['publication_delay_minutes']}m")


if __name__ == "__main__":
    main()
