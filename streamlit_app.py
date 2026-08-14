from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from jsonschema import Draft202012Validator, FormatChecker

SNAPSHOT_PATH = Path(__file__).with_name("public_snapshot.json")
SCHEMA_PATH = Path(__file__).with_name("public_snapshot.schema.json")
LOGGER = logging.getLogger("watchjoeylosemoney")

st.set_page_config(
    page_title="Watch Joey Lose Money",
    page_icon="\U0001F4C9",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    .wjlm-badge {
        display: inline-block;
        padding: 0.2rem 0.7rem;
        border-radius: 999px;
        background: rgba(127,127,127,0.15);
        font-weight: 600;
        font-size: 0.95rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def load_and_validate_snapshot() -> dict[str, Any]:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(snapshot), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5])
        raise ValueError(details)
    return snapshot


def money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def status_badge(snapshot: dict[str, Any]) -> None:
    mode = snapshot["mode"]
    status = snapshot["status"]
    labels = {
        "SHADOW": "\U0001F7E3 SHADOW — live data, no broker orders",
        "DEMO": "\U0001F7E1 DEMO — simulated execution",
        "LIVE": "\U0001F534 LIVE — real money",
        "PAUSED": "⏸️ PAUSED",
        "FAULT": "\U0001F6A8 FAULT",
        "OFFLINE": "⚫ OFFLINE",
    }
    label = labels.get(mode, mode)
    if status["data_delayed"]:
        label += f" · {snapshot['publication_delay_minutes']}m delayed"
    st.markdown(f'<span class="wjlm-badge">{label}</span>', unsafe_allow_html=True)
    st.caption(status["message"])


def render_metrics(snapshot: dict[str, Any]) -> None:
    s = snapshot["stats"]
    cols = st.columns(6)
    cols[0].metric("Joey's money*", money(s["display_equity_usd"]), money(s["realized_pnl_today_usd"]))
    cols[1].metric("All-time P&L", money(s["realized_pnl_all_time_usd"]))
    cols[2].metric("Current drawdown", money(-s["current_drawdown_usd"]))
    cols[3].metric("Max drawdown", money(-s["max_drawdown_usd"]))
    cols[4].metric("Trades", f"{s['closed_trades_all_time']:,}", f"{s['closed_trades_today']} today")
    cols[5].metric("Expectancy", money(s["expectancy_usd"]), f"Win rate {pct(s['win_rate'])}")
    st.caption(
        "*Presentation equity = configured public starting baseline + sanitized realized P&L; "
        "it is not broker net liquidation value."
    )


def render_charts(snapshot: dict[str, Any]) -> None:
    curve = pd.DataFrame(snapshot["equity_curve"])
    if curve.empty:
        st.info("No closed public trades yet.")
        return
    curve["ts_utc"] = pd.to_datetime(curve["ts_utc"], utc=True)
    st.subheader("Equity curve")
    st.line_chart(curve, x="ts_utc", y="equity_usd", x_label="UTC", y_label="Display equity ($)", height=320)

    st.subheader("Daily P&L")
    # Resample to one point per calendar day (last known cumulative P&L
    # that day, carried forward across no-trade days) rather than one
    # point per trade -- shows overall profit, positive or negative, as
    # a daily trajectory instead of the per-trade drawdown view.
    daily = (
        curve.set_index("ts_utc")["cum_pnl_usd"]
        .resample("1D")
        .last()
        .ffill()
        .reset_index()
        .rename(columns={"ts_utc": "date", "cum_pnl_usd": "cumulative_pnl_usd"})
    )
    st.line_chart(daily, x="date", y="cumulative_pnl_usd", x_label="Date (UTC)", y_label="Cumulative P&L ($)", height=280)

    st.subheader("Drawdown")
    st.line_chart(curve, x="ts_utc", y="drawdown_usd", x_label="UTC", y_label="Drawdown ($)", height=240)


def render_trade_table(snapshot: dict[str, Any]) -> None:
    trades = pd.DataFrame(snapshot["latest_trades"])
    st.subheader("Latest closed trades")
    if trades.empty:
        st.info("No closed public trades yet.")
        return
    trades["duration_min"] = trades["duration_seconds"] / 60.0
    table = trades[["closed_at_utc", "mode", "side", "exit_reason", "duration_min", "pnl_usd"]].copy()
    table["closed_at_utc"] = pd.to_datetime(table["closed_at_utc"], utc=True)
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "closed_at_utc": st.column_config.DatetimeColumn("Closed (UTC)", format="YYYY-MM-DD HH:mm:ss"),
            "mode": st.column_config.TextColumn("Mode"),
            "side": st.column_config.TextColumn("Side"),
            "exit_reason": st.column_config.TextColumn("Exit"),
            "duration_min": st.column_config.NumberColumn("Minutes", format="%.1f"),
            "pnl_usd": st.column_config.NumberColumn("P&L", format="$%.2f"),
        },
    )


def render_execution_telemetry(snapshot: dict[str, Any]) -> None:
    t = snapshot["execution_telemetry"]
    st.subheader("Execution quality")
    if t["sample_size"] == 0:
        st.info("No real-fill execution samples yet.")
        return
    cols = st.columns(6)
    cols[0].metric("Samples", f"{t['sample_size']:,}")
    cols[1].metric("Median submit→ACK", f"{t['submit_to_ack_ms_median']:.0f} ms" if t["submit_to_ack_ms_median"] is not None else "—")
    cols[2].metric("p95 submit→ACK", f"{t['submit_to_ack_ms_p95']:.0f} ms" if t["submit_to_ack_ms_p95"] is not None else "—")
    cols[3].metric("Median submit→fill", f"{t['submit_to_fill_ms_median']:.0f} ms" if t["submit_to_fill_ms_median"] is not None else "—")
    cols[4].metric("Mean slippage", f"{t['slippage_ticks_mean']:.2f} ticks" if t["slippage_ticks_mean"] is not None else "—")
    cols[5].metric("p95 slippage", f"{t['slippage_ticks_p95']:.2f} ticks" if t["slippage_ticks_p95"] is not None else "—")


@st.fragment(run_every="30s")
def live_dashboard() -> None:
    try:
        snapshot = load_and_validate_snapshot()
    except Exception:
        LOGGER.exception("Public snapshot validation/render input failed")
        st.error(
            "Public snapshot is temporarily unavailable. "
            "The dashboard is failing closed rather than rendering unvalidated data."
        )
        return

    status_badge(snapshot)
    render_metrics(snapshot)

    if snapshot["mode"] == "LIVE":
        st.info(
            "LIVE results are deliberately delayed and closed-trade-only. Current positions, "
            "exact entries, stops, targets, order IDs, and account details are never published."
        )

    render_charts(snapshot)
    render_trade_table(snapshot)
    render_execution_telemetry(snapshot)


st.title("watch joey lose money")
st.caption("An unnecessarily sophisticated system for losing $0.50 at a time.")

live_dashboard()
