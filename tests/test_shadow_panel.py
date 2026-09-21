"""dashboard_extras.t1_shadow_controllers: passes the schema, renders in the app, and stays absent-safe."""
import json
import shutil
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {"threshold", "thresholds", "entry_price", "exit_price", "stop_price", "session", "date", "events", "E", "R", "cap"}


def _variant(total, vs, exits, re, worst):
    return {"total_pts": total, "vs_hold_pts": vs, "exits": exits, "reentries": re, "worst_mark_pts": worst}


def _block(n):
    v = {"pess": _variant(300.0, -66.0, 2, 1, -55.0), "alt": _variant(310.0, -56.0, 2, 2, -60.0)}
    cum = list(range(100, 100 + 100 * n, 100))
    return {"episodes": n, "unit": "points", "hold_total_pts": 366.0, "variants": {"C2": v, "C3": v},
            "cumulative": {"HOLD": cum, "C2": cum, "C3": cum}}


def _snapshot(extras):
    snap = json.loads((ROOT / "public_snapshot.json").read_text(encoding="utf-8"))
    snap["dashboard_extras"] = extras
    return snap


def _render(tmp_path, snap):
    for name in ("streamlit_app.py", "public_snapshot.schema.json"):
        shutil.copy(ROOT / name, tmp_path / name)
    (tmp_path / "public_snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
    at = AppTest.from_file(str(tmp_path / "streamlit_app.py"), default_timeout=60).run()
    return at


def test_schema_accepts_shadow_block():
    schema = json.loads((ROOT / "public_snapshot.schema.json").read_text(encoding="utf-8"))
    errs = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(_snapshot({"t1_shadow_controllers": _block(3)})))
    assert not errs, errs


def test_shadow_block_carries_no_internals():
    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k; yield from keys(v)
    assert not (set(keys(_block(2))) & FORBIDDEN)


def test_panel_renders_with_episodes(tmp_path):
    at = _render(tmp_path, _snapshot({"t1_shadow_controllers": _block(3)}))
    assert not at.exception, at.exception
    assert any("Shadow exit/re-entry" in s.value for s in at.subheader)
    assert len(at.dataframe) >= 1


def test_panel_waiting_state_with_zero_episodes(tmp_path):
    blk = {"episodes": 0, "unit": "points", "hold_total_pts": 0.0, "variants": {}, "cumulative": {}}
    at = _render(tmp_path, _snapshot({"t1_shadow_controllers": blk}))
    assert not at.exception, at.exception
    assert any("Waiting for the first completed signal day" in i.value for i in at.info)


def test_panel_absent_when_no_extras(tmp_path):
    at = _render(tmp_path, _snapshot({}))
    assert not at.exception, at.exception
    assert not any("Shadow exit/re-entry" in s.value for s in at.subheader)
