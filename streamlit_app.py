from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import altair as alt
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


def format_pst(iso_utc: str) -> str:
    """publish.sh's cron runs every 15 min -- this reflects that cadence,
    not true real-time, so it's labeled "Last updated" (data freshness),
    not "Last refreshed" (page load time)."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    pst = dt.astimezone(ZoneInfo("America/Los_Angeles"))
    return pst.strftime("%Y-%m-%d %I:%M:%S %p %Z")


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
    in_position = status.get("in_position")
    if in_position is True:
        label += " · \U0001F535 in position"
    elif in_position is False:
        label += " · ⚪ flat, watching"
    st.markdown(f'<span class="wjlm-badge">{label}</span>', unsafe_allow_html=True)
    st.caption(status["message"])


def render_metrics(snapshot: dict[str, Any]) -> None:
    s = snapshot["stats"]
    cols = st.columns(6)
    cols[0].metric("Cumulative P&L*", money(s["display_equity_usd"]), money(s["realized_pnl_today_usd"]))
    cols[1].metric("All-time P&L", money(s["realized_pnl_all_time_usd"]))
    cols[2].metric("Current drawdown", money(-s["current_drawdown_usd"]))
    cols[3].metric("Max drawdown", money(-s["max_drawdown_usd"]))
    cols[4].metric("Trades", f"{s['closed_trades_all_time']:,}", f"{s['closed_trades_today']} today")
    cols[5].metric("Expectancy", money(s["expectancy_usd"]), f"Win rate {pct(s['win_rate'])}")
    st.caption(
        "*Starting from a presentation baseline of $0 (not the real broker balance) so losses "
        "show as negative before any gains offset them, rather than being hidden inside a "
        "cushion. Real-money accounting still happens on the broker side."
    )
    ledger_adj = s.get("ledger_adjustment_usd", 0.0)
    if ledger_adj:
        direction = "credit" if ledger_adj > 0 else "debit"
        st.caption(
            f"Includes a documented {money(ledger_adj)} {direction} correction for a bug-affected "
            "trade (forced exit from an operational bug, not a strategy decision) -- the raw trade "
            "record is unchanged, only this total is corrected."
        )


_QUALIFICATION_TARGET_TRADES = 1500  # max(6wk, 1500 trades) per the frozen forward-qualification protocol



def render_charts(snapshot: dict[str, Any]) -> None:
    curve = pd.DataFrame(snapshot["equity_curve"])
    if curve.empty:
        st.info("No closed public trades yet.")
        return
    curve["ts_utc"] = pd.to_datetime(curve["ts_utc"], utc=True)

    def _resampled_cum_pnl(rule: str, label: str) -> pd.DataFrame:
        # One point per period (last known cumulative P&L that period,
        # carried forward across no-trade periods) rather than one point
        # per trade -- shows overall profit, positive or negative, as a
        # trajectory instead of the per-trade drawdown view.
        return (
            curve.set_index("ts_utc")["cum_pnl_usd"]
            .resample(rule)
            .last()
            .ffill()
            .reset_index()
            .rename(columns={"ts_utc": label, "cum_pnl_usd": "cumulative_pnl_usd"})
        )

    st.subheader("Hourly P&L")
    hourly = _resampled_cum_pnl("1h", "hour")
    st.line_chart(hourly, x="hour", y="cumulative_pnl_usd", x_label="Hour (your local timezone)", y_label="Cumulative P&L ($)", height=280)

    st.subheader("Daily P&L")
    daily = _resampled_cum_pnl("1D", "date")
    st.line_chart(daily, x="date", y="cumulative_pnl_usd", x_label="Date (your local timezone)", y_label="Cumulative P&L ($)", height=280)

    st.subheader("Drawdown")
    st.line_chart(curve, x="ts_utc", y="drawdown_usd", x_label="Your local timezone", y_label="Drawdown ($)", height=240)


_WEEKDAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def render_daily_pnl_heatmap(snapshot: dict[str, Any]) -> None:
    curve = pd.DataFrame(snapshot["equity_curve"])
    st.subheader("Daily P&L calendar")
    if curve.empty:
        st.info("No closed public trades yet.")
        return
    curve["ts_utc"] = pd.to_datetime(curve["ts_utc"], utc=True)

    # One value per calendar day: last-known cumulative P&L that day (carried
    # forward across no-trade days via ffill, same convention as the line
    # charts above), then differenced to get that day's own net P&L.
    daily_cum = curve.set_index("ts_utc")["cum_pnl_usd"].resample("1D").last().ffill()
    daily_pnl = daily_cum.diff()
    daily_pnl.iloc[0] = daily_cum.iloc[0]  # first day's P&L is its cum value from the $0 baseline
    df = daily_pnl.reset_index()
    df.columns = ["date", "pnl"]
    df["week_start"] = df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")
    df["weekday"] = df["date"].dt.weekday.map(dict(enumerate(_WEEKDAY_ORDER)))

    max_abs = max(abs(df["pnl"].min()), abs(df["pnl"].max()), 1.0)
    chart = (
        alt.Chart(df)
        .mark_rect(cornerRadius=3, stroke="#00000014", strokeWidth=1)
        .encode(
            x=alt.X("week_start:T", timeUnit="yearmonthdate", title=None, axis=alt.Axis(format="%b %d", grid=False)),
            y=alt.Y("weekday:N", title=None, sort=_WEEKDAY_ORDER),
            color=alt.Color(
                "pnl:Q",
                title="Daily P&L ($)",
                scale=alt.Scale(domain=[-max_abs, 0, max_abs], range=["#e34948", "#f0efec", "#2a78d6"]),
            ),
            tooltip=[
                alt.Tooltip("date:T", title="Date", format="%Y-%m-%d"),
                alt.Tooltip("pnl:Q", title="P&L", format="$,.2f"),
            ],
        )
        .properties(height=180)
    )
    st.altair_chart(chart, width="stretch")
    st.caption(
        "Each cell is one calendar day (UTC), colored by that day's net P&L -- blue for gains, red for "
        "losses, pale gray near $0. A day with no trades shows as $0 (flat), same color as a day that "
        "traded and broke even; hover a cell for the exact date and dollar figure."
    )


def render_trade_table(snapshot: dict[str, Any]) -> None:
    trades = pd.DataFrame(snapshot["latest_trades"])
    st.subheader("Latest closed trades")
    if trades.empty:
        st.info("No closed public trades yet.")
        return
    trades["duration_min"] = trades["duration_seconds"] / 60.0
    # reindex, not direct [[...]] column selection -- a code deploy can land
    # on Streamlit Cloud before the next cron-published public_snapshot.json
    # carries a brand-new field (data updates on a ~15min cycle, independent
    # of when code deploys), so a column this code expects can be legitimately
    # absent for a few minutes. reindex fills it with NaN instead of raising
    # KeyError and taking the whole page down (real incident, 2026-09-08).
    table = trades.reindex(columns=[
        "closed_at_utc", "mode", "side", "exit_reason", "duration_min", "pnl_usd", "sized_qty", "mfe_atr", "mae_atr",
    ]).copy()
    table["closed_at_utc"] = pd.to_datetime(table["closed_at_utc"], utc=True).dt.tz_convert("America/Los_Angeles")
    st.caption(
        "Most recent 25 closed trades. \"Qty\" is the MNQ-equivalent size that trade was sized at. "
        "MFE/MAE (in ATR units) are blank for trades closed before 2026-09-08's telemetry was added."
    )
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "closed_at_utc": st.column_config.DatetimeColumn("Closed (PST)", format="YYYY-MM-DD HH:mm:ss"),
            "mode": st.column_config.TextColumn("Mode"),
            "side": st.column_config.TextColumn("Side"),
            "exit_reason": st.column_config.TextColumn("Exit"),
            "duration_min": st.column_config.NumberColumn("Minutes", format="%.1f"),
            "pnl_usd": st.column_config.NumberColumn("P&L", format="$%.2f"),
            "sized_qty": st.column_config.NumberColumn("Qty", format="%.0f"),
            "mfe_atr": st.column_config.NumberColumn("MFE/ATR", format="%.2f"),
            "mae_atr": st.column_config.NumberColumn("MAE/ATR", format="%.2f"),
        },
    )

def render_reliability(snapshot: dict[str, Any]) -> None:
    r = snapshot["reliability"]
    st.subheader("Process reliability")
    cols = st.columns(3)
    cols[0].metric("Restarts", f"{r['restart_count']:,}" if r["restart_count"] is not None else "—")
    cols[1].metric("Crash reports", f"{r['crash_report_count']:,}")
    if r["last_started_at_utc"]:
        uptime = datetime.now(timezone.utc) - pd.to_datetime(r["last_started_at_utc"], utc=True).to_pydatetime()
        cols[2].metric("Uptime (current run)", f"{uptime.total_seconds() / 3600:.1f}h")
    else:
        cols[2].metric("Uptime (current run)", "—")
    if r["first_started_at_utc"]:
        st.caption(f"First started {r['first_started_at_utc']}. A restart count that keeps climbing without a matching crash report usually means a deliberate config change, not instability.")


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
    st.caption(f"Last updated: {format_pst(snapshot['generated_at_utc'])}")
    render_metrics(snapshot)

    if snapshot["mode"] == "LIVE":
        st.info(
            "LIVE results are deliberately delayed and closed-trade-only. Current positions, "
            "exact entries, stops, targets, order IDs, and account details are never published."
        )

    st.divider()
    st.subheader("Risk")
    render_charts(snapshot)

    st.divider()
    render_daily_pnl_heatmap(snapshot)
    render_trade_table(snapshot)
    render_reliability(snapshot)


st.title("watch joey lose money")
st.caption("An unnecessarily sophisticated system for losing $0.50 at a time.")

live_dashboard()
