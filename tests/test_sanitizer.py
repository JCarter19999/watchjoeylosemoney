import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sanitizer import build_public_snapshot, validate_snapshot  # noqa: E402

SCHEMA_PATH = ROOT / "public_snapshot.schema.json"


def _poison_private(mode: str = "DEMO", now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    closed_at = (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    return {
        "mode": mode,
        "service_state": "ARMED",
        "guardian_state": "ARMED",
        "market_data_state": "LIVE",
        "execution_enabled": True,
        "message": "Armed and watching for entries.",
        "display_starting_equity_usd": 5000.0,
        "accountId": "DO_NOT_LEAK",
        "api_key": "DO_NOT_LEAK",
        "trades": [
            {
                "closed_at_utc": closed_at,
                "side": "long",
                "pnl_usd": 12.5,
                "exit_reason": "TARGET",
                "duration_seconds": 120,
                "mode": mode,
                "orderId": 123456,
                "open_position": {"side": "LONG", "qty": 10, "entry_price": 30125.25},
                "entry_price": 30125.25,
                "stop_price": 30100.0,
                "target_price": 30150.0,
                "entry_submit_to_fill_observed_ms": 300.0,
                "signal_to_order_latency_ms": 150.0,
                "entry_slippage_dollars": 1.0,
                "exit_slippage_dollars": 0.5,
            }
        ],
    }


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_sanitizer_never_leaks_poison_pill_fields() -> None:
    private = _poison_private()
    public = build_public_snapshot(private, datetime.now(timezone.utc), live_delay_minutes=15)
    validate_snapshot(public, SCHEMA_PATH)

    dumped = json.dumps(public)
    for forbidden in ("DO_NOT_LEAK", "accountId", "api_key", "orderId", "open_position", "entry_price", "stop_price", "target_price"):
        assert forbidden not in dumped, f"leaked forbidden value/key: {forbidden}"


def test_live_trade_is_embargoed_until_delay_expires() -> None:
    now = datetime.now(timezone.utc)
    private = _poison_private(mode="LIVE", now=now)
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    assert public["latest_trades"] == []  # closed 1 minute ago, delay is 15 minutes


def test_live_requires_minimum_delay() -> None:
    private = _poison_private(mode="LIVE")
    with pytest.raises(ValueError):
        build_public_snapshot(private, datetime.now(timezone.utc), live_delay_minutes=5)


def test_open_trade_never_published() -> None:
    now = datetime.now(timezone.utc)
    private = _poison_private(now=now)
    private["trades"].append({
        "closed_at_utc": None,
        "side": "short",
        "pnl_usd": 0.0,
        "exit_reason": None,
        "duration_seconds": None,
        "mode": "DEMO",
    })
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    assert len(public["latest_trades"]) == 1  # only the closed one


def _waterfall_trade(trade_id: str, exit_time_iso: str, model_pnl: float, realized_pnl: float) -> dict:
    drag = model_pnl - realized_pnl  # a+b+c+fees, split arbitrarily for test purposes
    return {
        "trade_id": trade_id, "side": "long", "exit_reason": "TARGET", "exit_time": exit_time_iso,
        "model_pnl_dollars": model_pnl,
        "a_semantic_reference_gap_dollars": drag * 0.5,
        "b_decision_to_submit_dollars": drag * 0.25,
        "c_execution_residual_dollars": drag * 0.25 - 1.0,
        "fees_dollars": -1.0,
        "realized_pnl_dollars": realized_pnl,
    }


def test_waterfall_aggregate_recomputed_over_visible_trades_only() -> None:
    """A DEMO-mode private snapshot has no embargo -- both trades visible,
    aggregate is the plain mean of both."""
    now = datetime.now(timezone.utc)
    private = _poison_private(now=now)
    private["pnl_waterfall"] = {
        "n_missing_decomposition": 0,
        "trades": [
            _waterfall_trade("t1", (now - timedelta(hours=2)).isoformat(), model_pnl=10.0, realized_pnl=6.0),
            _waterfall_trade("t2", (now - timedelta(hours=1)).isoformat(), model_pnl=20.0, realized_pnl=19.0),
        ],
    }
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    validate_snapshot(public, SCHEMA_PATH)

    assert public["pnl_waterfall"]["n_trades"] == 2
    assert public["pnl_waterfall"]["aggregate"]["model_expectancy_usd"] == pytest.approx(15.0)
    assert public["pnl_waterfall"]["aggregate"]["realized_expectancy_usd"] == pytest.approx(12.5)


def test_waterfall_embargoes_live_trades_and_excludes_from_aggregate() -> None:
    """A LIVE-mode trade inside the delay window must not appear in the
    published waterfall rows OR contribute to the published aggregate --
    the aggregate is recomputed over visible rows, not passed through."""
    now = datetime.now(timezone.utc)
    private = _poison_private(mode="LIVE", now=now)
    private["pnl_waterfall"] = {
        "n_missing_decomposition": 0,
        "trades": [
            _waterfall_trade("old", (now - timedelta(hours=2)).isoformat(), model_pnl=10.0, realized_pnl=6.0),
            _waterfall_trade("embargoed", (now - timedelta(minutes=1)).isoformat(), model_pnl=1000.0, realized_pnl=999.0),
        ],
    }
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    validate_snapshot(public, SCHEMA_PATH)

    assert public["pnl_waterfall"]["n_trades"] == 1
    assert all(t["trade_id"] != "embargoed" for t in public["pnl_waterfall"]["trades"])
    # aggregate must reflect ONLY the visible "old" trade, not the 1000/999 embargoed one
    assert public["pnl_waterfall"]["aggregate"]["model_expectancy_usd"] == pytest.approx(10.0)


def test_waterfall_defaults_empty_when_absent_from_private() -> None:
    now = datetime.now(timezone.utc)
    private = _poison_private(now=now)  # no "pnl_waterfall" key at all
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    validate_snapshot(public, SCHEMA_PATH)
    assert public["pnl_waterfall"]["n_trades"] == 0
    assert public["pnl_waterfall"]["trades"] == []


def test_slippage_by_exit_reason_grouped_and_median_resists_outlier() -> None:
    """STOP_TIGHTENED trades get one huge outlier plus one ordinary trade;
    TARGET gets two ordinary trades. Confirms grouping is by exit_reason,
    n is per-group, and the STOP_TIGHTENED median resists its own outlier
    the same way the blended median does."""
    now = datetime.now(timezone.utc)
    private = _poison_private(now=now)

    def trade(reason: str, entry_slip: float, exit_slip: float, minutes_ago: int):
        base = dict(private["trades"][0])
        base["closed_at_utc"] = (now - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
        base["exit_reason"] = reason
        base["entry_slippage_dollars"] = entry_slip
        base["exit_slippage_dollars"] = exit_slip
        return base

    private["trades"] = [
        trade("STOP_TIGHTENED", 0.0, 0.5, 10),
        trade("STOP_TIGHTENED", 0.0, 44.5, 9),  # huge outlier, 89 ticks total
        trade("TARGET", 0.0, 1.0, 8),
        trade("TARGET", 0.0, -1.0, 7),
    ]
    public = build_public_snapshot(private, now, live_delay_minutes=15)
    validate_snapshot(public, SCHEMA_PATH)

    by_reason = public["execution_telemetry"]["slippage_by_exit_reason"]
    assert by_reason["STOP_TIGHTENED"]["n"] == 2
    # _percentile is nearest-rank, not interpolated -- at n=2 it returns the
    # LOWER of the two sorted values (round(0.5*(2-1))=round(0.5)=0, banker's
    # rounding), i.e. 1.0, not the 89.0 outlier. Still demonstrates the
    # point: the outlier does not dominate the reported STOP_TIGHTENED figure.
    assert by_reason["STOP_TIGHTENED"]["median_ticks"] == pytest.approx(1.0)
    assert by_reason["STOP_TIGHTENED"]["mean_ticks"] == pytest.approx((1.0 + 89.0) / 2)  # mean DOES get dragged to 45
    assert by_reason["TARGET"]["n"] == 2
    assert by_reason["TARGET"]["median_ticks"] == pytest.approx(-2.0)  # nearest-rank lower value, see comment above
    assert by_reason["TARGET"]["mean_ticks"] == pytest.approx(0.0)
