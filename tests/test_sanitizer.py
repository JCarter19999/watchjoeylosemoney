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
