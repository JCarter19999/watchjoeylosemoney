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
            }
            for r in visible_trades[-MAX_LATEST_TRADES:][::-1]
        ],
    }
    return public


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
