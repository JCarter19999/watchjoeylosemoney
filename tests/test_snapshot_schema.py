import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_KEYS = {
    "account_id", "accountid", "account_number", "accountnumber",
    "order_id", "orderid", "exchange_order_id", "exchangeorderid",
    "access_token", "refresh_token", "api_key", "secret", "password",
    "ip", "ip_address", "entry_price", "stop_price", "target_price",
    "open_position", "broker_balance", "net_liq", "net_liquidation",
}


def _load_snapshot() -> dict:
    return json.loads((ROOT / "public_snapshot.json").read_text(encoding="utf-8"))


def _load_schema() -> dict:
    return json.loads((ROOT / "public_snapshot.schema.json").read_text(encoding="utf-8"))


def test_public_snapshot_matches_schema() -> None:
    snapshot = _load_snapshot()
    schema = _load_schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(snapshot), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_live_mode_enforces_delay_floor() -> None:
    snapshot = _load_snapshot()
    if snapshot["mode"] == "LIVE":
        assert snapshot["publication_delay_minutes"] >= 10
        assert snapshot["status"]["data_delayed"] is True
        generated = datetime.fromisoformat(snapshot["generated_at_utc"].replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(snapshot["data_as_of_utc"].replace("Z", "+00:00"))
        assert generated > as_of


def _walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def test_no_forbidden_keys_in_public_snapshot() -> None:
    snapshot = _load_snapshot()
    keys = {str(k).lower() for k in _walk(snapshot) if isinstance(k, str)}
    leaked = keys & FORBIDDEN_KEYS
    assert not leaked, f"forbidden keys present in public snapshot: {leaked}"


def test_latest_trades_capped_at_25() -> None:
    snapshot = _load_snapshot()
    assert len(snapshot["latest_trades"]) <= 25
