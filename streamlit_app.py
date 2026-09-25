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
    render_unrealized(snapshot)


def render_unrealized(snapshot: dict[str, Any]) -> None:
    """Live, throughout-the-day mark-to-market P/L on any currently open position(s) -- MNQ from status.
    unrealized_pnl_usd, 6J (if that leg is running) from dashboard_extras.t1_6j_leg.unrealized_pnl_usd. Both are
    None (shown as nothing) when flat, and always None for a real-money LIVE deployment regardless of position --
    unlike DEMO/SHADOW, LIVE never discloses position-level detail in real time (see the schema's own note on
    status.unrealized_pnl_usd)."""
    rows = []
    mnq_u = snapshot["status"].get("unrealized_pnl_usd")
    if mnq_u is not None:
        rows.append(("T1 / MNQ", mnq_u))
    leg = (snapshot.get("dashboard_extras") or {}).get("t1_6j_leg")
    if leg and leg.get("unrealized_pnl_usd") is not None:
        rows.append(("6J London", leg["unrealized_pnl_usd"]))
    if not rows:
        return
    cols = st.columns(len(rows))
    for col, (label, val) in zip(cols, rows):
        col.metric(f"{label} — unrealized", money(val))
    st.caption("Mark-to-market off the last received bar, not a fill -- no commission or slippage included, and it moves every minute the market does.")
    if leg and leg.get("unrealized_pnl_adjusted"):
        st.info(
            f"6J's unrealized figure above is a documented correction, not the raw broker number: the actual "
            f"Tradovate fill would show **{money(leg['raw_unrealized_pnl_usd'])}**. {leg['unrealized_adjustment_reason']}"
        )
    render_fill_slippage_check(leg)


def render_fill_slippage_check(leg: dict[str, Any] | None) -> None:
    """Surfaces the 6J leg's most recent fill-vs-market-reference check (dashboard_extras.t1_6j_leg.
    last_fill_slippage_check), added 2026-09-22 after a genuine Tradovate DEMO fill was found 59 ticks off the
    contemporaneous market on 6J's first-ever live fill -- a demo fill-simulation artifact, not real 6J
    liquidity. Flags it plainly rather than letting a distorted fill silently sit inside the P&L numbers
    above. Absent entirely once no fill has happened yet."""
    check = (leg or {}).get("last_fill_slippage_check")
    if not check:
        return
    label = "entry" if check["event"] == "entry_slippage_check" else "exit"
    msg = (f"6J {label} fill vs. market reference: filled {check['fill_price']} against a reference of "
          f"{check['market_reference']} ({check['slippage_ticks']:+.1f} ticks, {money(check['slippage_usd'])}).")
    if check["anomalous"]:
        st.warning(
            f"⚠️ {msg} This is well outside normal execution noise and looks like a Tradovate DEMO "
            "fill-simulation artifact rather than real market slippage -- displayed P&L on this leg may be "
            "distorted by it. See the 2026-09-22 finding."
        )
    else:
        st.caption(msg + " Within normal range.")


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
    # timestamps mix fixed-second and microsecond-precision ISO strings (some snapshot writers
    # round to the second, others don't) -- format="ISO8601" parses per-element instead of
    # inferring one fixed format from the first value and applying it array-wide, which breaks
    # on that mix under pandas' fast strptime path.
    curve["ts_utc"] = pd.to_datetime(curve["ts_utc"], utc=True, format="ISO8601")

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
    curve["ts_utc"] = pd.to_datetime(curve["ts_utc"], utc=True, format="ISO8601")

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
    table["closed_at_utc"] = pd.to_datetime(table["closed_at_utc"], utc=True, format="ISO8601").dt.tz_convert("America/Los_Angeles")
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

def render_contract_distribution(snapshot: dict[str, Any]) -> None:
    """Current live order_qty per leg (dashboard_extras.contract_distribution) -- what size each strategy is
    running right now, separate from the closed-trade ledger. A leg running in SHADOW (OBSERVE, no real orders
    -- e.g. ON-001/ES as of 2026-09-24) is labeled explicitly rather than looking like a live position. Absent
    entirely on an older snapshot or if this leg's status file isn't present."""
    dist = (snapshot.get("dashboard_extras") or {}).get("contract_distribution")
    if not dist or not dist.get("legs"):
        return
    st.subheader("Current contract distribution")
    labels = {"t1": "T1 / MNQ", "6j": "6J London", "le": "LE Cattle", "on001": "ON-001 / ES", "eia_ho": "EIA sleeve: HO", "eia_rb": "EIA sleeve: RB"}
    legs = dist["legs"]
    cols = st.columns(len(legs))
    for col, (key, leg) in zip(cols, legs.items()):
        label = labels.get(key, key)
        qty = leg.get("order_qty")
        suffix = f" ({leg['controller']})" if leg.get("controller") else ""
        value = f"{qty}{suffix}" if qty is not None else "—"
        if leg.get("live") is False:
            col.metric(label, value, delta="SHADOW", delta_color="off")
        else:
            col.metric(label, value)
    sleeve_note = " HO and RB are ONE EIA petroleum-report sleeve (same event and Wednesdays, trade P&L correlation about +0.6) -- one event exposure, not two independent legs; max 1 contract each." if any(v.get("sleeve") == "EIA" for v in legs.values()) else ""
    st.caption(f"As of {format_pst(dist['as_of_utc'])}. Reflects live order size right now, not the size any past trade in the ledger below was taken at. SHADOW = tracked for research, no real orders placed.{sleeve_note}")


def render_projection(snapshot: dict[str, Any]) -> None:
    """Projected annualized P&L + historical-window MDD, split into the FUNDED wheel (T1+6J, real live capital)
    and ES as a separate SHADOW projection (dashboard_extras.projection, written by
    scripts/project_annualized_mdd.py). Switched to this split 2026-09-24 when ES moved from live-executing to
    shadow-only. A BACKTEST projection, not the account's actual realized rate -- explicitly labeled as such."""
    proj = (snapshot.get("dashboard_extras") or {}).get("projection")
    if not proj:
        return
    st.subheader("Projected annualized P&L + max drawdown")
    c = proj.get("contracts", {})
    funded = proj.get("funded")
    if funded:
        st.caption(f"Funded wheel (real live capital): {c.get('t1_mnq')} MNQ / {c.get('sixj')} 6J" + (f" / {c.get('le')} LE cattle" if c.get('le') else "") + (f" / EIA sleeve {c.get('eia_ho', 0)} HO + {c.get('eia_rb', 0)} RB" if (c.get('eia_ho') or c.get('eia_rb')) else ""))
        col1, col2 = st.columns(2)
        col1.metric("Projected annualized P&L", money(funded["projected_annualized_pnl_usd"]))
        col2.metric("Historical-window max drawdown", money(funded["window_max_drawdown_usd"]))
        if funded.get("reordered_mdd_p50_usd") is not None:
            st.caption("The historical drawdown is the ONE ordering that happened, and it was on the lucky end. Re-shuffling the same days "
                       "gives the drawdowns to actually plan around:")
            r1, r2, r3 = st.columns(3)
            r1.metric("Typical (P50)", money(funded["reordered_mdd_p50_usd"]))
            r2.metric("Bad (1 in 10)", money(funded["reordered_mdd_p90_usd"]))
            r3.metric("Very bad (1 in 20)", money(funded["reordered_mdd_p95_usd"]))
        es_shadow = proj.get("es_shadow")
        if es_shadow:
            st.caption(f"ON-001/ES — SHADOW only ({c.get('on001_es_shadow')} contract, no real capital):")
            col3, col4 = st.columns(2)
            col3.metric("Shadow projected annualized P&L", money(es_shadow["projected_annualized_pnl_usd"]))
            col4.metric("Shadow historical-window MDD", money(es_shadow["window_max_drawdown_usd"]))
        combined = proj.get("combined_if_es_live")
        if combined:
            st.caption(f"For reference only, NOT current sizing — if ES's shadow qty were promoted to live: "
                      f"annualized {money(combined['projected_annualized_pnl_usd'])}, "
                      f"MDD {money(combined['window_max_drawdown_usd'])}.")
    else:
        # older snapshot shape (pre-shadow-split): flat fields, no funded/es_shadow breakdown
        st.caption(f"At current live sizing: {c.get('t1_mnq')} MNQ / {c.get('sixj')} 6J / {c.get('on001_es')} ES")
        col1, col2 = st.columns(2)
        col1.metric("Projected annualized P&L", money(proj.get("projected_annualized_pnl_usd", 0)))
        col2.metric("Historical-window max drawdown", money(proj.get("window_max_drawdown_usd", 0)))
    st.caption(proj.get("caveat", ""))


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
    render_contract_distribution(snapshot)
    render_projection(snapshot)

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
