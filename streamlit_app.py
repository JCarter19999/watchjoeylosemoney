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
        "pnl_adjusted", "raw_pnl_usd", "v_cohort_label",
    ]).copy()
    # FAST_EXPANDED_V1 only -- was this trade also selected by the
    # original, already-live DV_SIGNAL_V1 rule (V5_CORE) or only once the
    # V_t gate was relaxed away (a *_MARGINAL cohort)? Blank/NaN for any
    # strategy with no V-relaxation concept (e.g. historical DV_SIGNAL_V1
    # rows), same reindex-fills-NaN convention as mfe_atr/mae_atr above.
    table["v_cohort_label"] = table["v_cohort_label"].fillna("")
    table["closed_at_utc"] = pd.to_datetime(table["closed_at_utc"], utc=True).dt.tz_convert("America/Los_Angeles")
    # 2026-09-10: a trade whose displayed P&L is a documented, authorized
    # correction (not its real fill outcome -- e.g. refunding a tooling-
    # bug's dollar impact) must never be visually indistinguishable from an
    # ordinary real trade. Two unrelated trades landing on the same dollar
    # figure by pure market coincidence is exactly the scenario that
    # otherwise reads as fabricated data. See sanitizer.py's pnl_adjusted/
    # raw_pnl_usd fields.
    adjusted_mask = table["pnl_adjusted"].fillna(False).astype(bool)
    table["adjusted_note"] = ""
    if adjusted_mask.any():
        table.loc[adjusted_mask, "adjusted_note"] = table.loc[adjusted_mask, "raw_pnl_usd"].map(
            lambda v: f"Adjusted -- real result was ${v:,.2f}" if pd.notna(v) else "Adjusted"
        )
    st.caption(
        "Most recent 25 closed trades. \"Qty\" is the MNQ-equivalent size that trade was sized at. "
        "MFE/MAE (in ATR units) are blank for trades closed before 2026-09-08's telemetry was added. "
        "A trade marked ⚠️ Adjusted shows a documented, human-authorized correction to its displayed "
        "P&L (e.g. refunding a since-fixed tooling bug's dollar impact) -- hover the note for the real result; "
        "the underlying raw trade record itself is never altered."
    )
    if adjusted_mask.any():
        st.dataframe(
            table.drop(columns=["pnl_adjusted", "raw_pnl_usd"]),
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
                "v_cohort_label": st.column_config.TextColumn("V Cohort"),
                "adjusted_note": st.column_config.TextColumn("⚠️ Adjusted"),
            },
        )
    else:
        st.dataframe(
            table.drop(columns=["pnl_adjusted", "raw_pnl_usd", "adjusted_note"]),
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
                "v_cohort_label": st.column_config.TextColumn("V Cohort"),
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


def render_shadow_controllers(snapshot: dict[str, Any]) -> None:
    """Optional panel (dashboard_extras.t1_shadow_controllers): aggregate points only. Absent for strategies without it."""
    sh = (snapshot.get("dashboard_extras") or {}).get("t1_shadow_controllers")
    if not sh:
        return
    st.divider()
    st.subheader("Shadow exit/re-entry variants (research only)")
    st.caption(
        "The demo bot trades one candidate exit/re-entry rule (C3). C2 and the plain hold-to-close rule are replayed next to it "
        "on every signal day for comparison (this table is a replay, not the fills). Points, not dollars. The rules were picked "
        "on historical data, so they are unproven until many more live signal days accumulate."
    )
    n = int(sh.get("episodes", 0))
    if n == 0:
        st.info("Waiting for the first completed signal day.")
        return
    rows = [{"variant": "Hold to close (certified rule)", "ordering": "-", "total pts": sh["hold_total_pts"], "vs hold": 0.0,
             "exits": 0, "re-entries": 0, "worst intraday mark": None}]
    for name, by_order in sh["variants"].items():
        for order, label in (("pess", "conservative fills"), ("alt", "alternate fills")):
            v = by_order[order]
            rows.append({"variant": name, "ordering": label, "total pts": v["total_pts"], "vs hold": v["vs_hold_pts"],
                         "exits": v["exits"], "re-entries": v["reentries"], "worst intraday mark": v["worst_mark_pts"]})
    st.caption(f"{n} completed signal day{'s' if n != 1 else ''}. Two fill orderings are shown because one-minute bars hide the true intrabar order.")
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    cum = sh.get("cumulative") or {}
    if n >= 2 and cum:
        chart = pd.DataFrame({k: v for k, v in cum.items() if len(v) == n}, index=range(1, n + 1))
        chart.index.name = "signal day #"
        st.line_chart(chart)


def render_6j_leg(snapshot: dict[str, Any]) -> None:
    """Optional panel (dashboard_extras.t1_6j_leg): the second, independent instrument the fused bot trades
    alongside T1/MNQ -- 6J.v.0 (Japanese Yen futures), a London-open opening-burst analogue of the same rule,
    HOLD-only (no exit/re-entry controller -- a dedicated study found the controller doesn't help this
    instrument). Absent entirely for a snapshot from before this leg existed, or if it isn't running."""
    leg = (snapshot.get("dashboard_extras") or {}).get("t1_6j_leg")
    if not leg:
        return
    st.divider()
    st.subheader("6J (Japanese Yen) leg")
    st.caption(
        "A second, independent T1-rule instrument running alongside the MNQ bot in the same process: 08:00 London-open "
        "anchor instead of 09:30 ET, held to 15:59 America/Chicago close, no exit/re-entry controller. Unproven -- this "
        "is its first live demo period, with no forward track record yet."
    )
    cols = st.columns(4)
    cols[0].metric("Phase", leg.get("phase") or "—")
    cols[1].metric("Contracts", leg.get("order_qty"))
    cols[2].metric("Closed trades", leg.get("closed_trades", 0))
    pnl = leg.get("total_pnl_usd")
    cols[3].metric("Total P&L (closed)", f"${pnl:,.2f}" if pnl is not None else "—")
    if leg.get("stream_health") not in (None, "HEALTHY", "StreamHealth.HEALTHY"):
        st.warning(f"6J data stream health: {leg.get('stream_health')}")
    if leg.get("n_alerts"):
        st.warning(f"{leg['n_alerts']} alert(s) logged for the 6J leg.")
    lt = leg.get("last_trade")
    if lt:
        st.caption(f"Last closed trade: {lt['session']} {lt['side']} -> ${lt['pnl_usd']:,.2f} ({lt['closed_at_utc']})")


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
    render_shadow_controllers(snapshot)
    render_6j_leg(snapshot)
    render_reliability(snapshot)


st.title("watch joey lose money")
st.caption("An unnecessarily sophisticated system for losing $0.50 at a time.")

live_dashboard()
