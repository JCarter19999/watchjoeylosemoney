from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import requests
import streamlit as st
from jsonschema import Draft202012Validator, FormatChecker

SNAPSHOT_PATH = Path(__file__).with_name("public_snapshot.json")
SCHEMA_PATH = Path(__file__).with_name("public_snapshot.schema.json")
LOGGER = logging.getLogger("watchjoeylosemoney")

# 2026-10-21: the deployed app was showing stale data for hours after real trades -- root cause
# was NOT the publish.sh cron (confirmed running and pushing to GitHub reliably every 15min) but
# this hosted (Streamlit Community Cloud) deployment only seeing a new commit whenever ITS OWN
# redeploy-on-push webhook happens to fire, which is not guaranteed to track every single push from
# an every-15-minutes cron. Fetching the raw file straight from GitHub on every 30s fragment rerun
# removes that dependency entirely -- every refresh gets whatever is actually on `main` right now,
# regardless of whether this deployment's own checkout has been redeployed. Local file is the
# fallback only (offline dev, or GitHub transiently unreachable), not the primary source anymore.
_SNAPSHOT_RAW_URL = "https://raw.githubusercontent.com/JCarter19999/watchjoeylosemoney/main/public_snapshot.json"

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
        padding: 0.25rem 0.8rem;
        border-radius: 999px;
        background: rgba(79, 143, 214, 0.16);
        border: 1px solid rgba(79, 143, 214, 0.28);
        font-weight: 600;
        font-size: 0.95rem;
        letter-spacing: 0.01em;
    }
    div[data-testid="stMetric"] {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
        padding: 0.85rem 1rem 0.6rem;
    }
    div[data-testid="stMetricLabel"] {
        opacity: 0.75;
    }
    hr {
        margin: 2rem 0 1.5rem 0;
        opacity: 0.12;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _fetch_live_snapshot_text() -> str:
    """Always-fresh source of truth: GitHub's raw content, cache-busted per call so neither
    GitHub's own CDN nor anything in between serves a stale cached copy. Falls back to the local
    on-disk file (this deployment's last-redeployed copy) only if the live fetch itself fails."""
    try:
        resp = requests.get(_SNAPSHOT_RAW_URL, params={"_": str(datetime.now(timezone.utc).timestamp())}, timeout=5)
        resp.raise_for_status()
        return resp.text
    except Exception:
        LOGGER.warning("Live snapshot fetch failed, falling back to local on-disk copy", exc_info=True)
        return SNAPSHOT_PATH.read_text(encoding="utf-8")


def load_and_validate_snapshot() -> dict[str, Any]:
    snapshot = json.loads(_fetch_live_snapshot_text())
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
    """Live, throughout-the-day mark-to-market P/L on any currently open position(s) -- MNQ from
    status.unrealized_pnl_usd, every other live leg from dashboard_extras.{leg}_leg.unrealized_pnl_usd.
    All are None (shown as nothing) when flat, and always None for a real-money LIVE deployment
    regardless of position -- unlike DEMO/SHADOW, LIVE never discloses position-level detail in real
    time (see the schema's own note on status.unrealized_pnl_usd).

    6J/LE/PA were disabled 2026-10-15 (see market_structure_ml-live/CLAUDE.md) and are deliberately not
    shown here anymore -- removed, not hidden, since those legs are no longer trading at all. EIA and
    ON-001/ES dropped 2026-10-16 for the same reason: not robust enough to keep running alongside the
    stronger legs. MGC10 and M6E both retired 2026-10-27 (user: "we are not running MGC10 anymore" /
    "get rid of M6E too, we are no longer using it") -- their real demo positions were closed (MGC10
    manually; M6E's own bracket had already resolved naturally before it could be confirmed) and
    both processes stopped; see wheel_v1_mgc/wheel_v1_mnq below for MGC10's replacement."""
    rows = []
    mnq_u = snapshot["status"].get("unrealized_pnl_usd")
    if mnq_u is not None:
        rows.append(("T1 / MNQ", mnq_u))
    for key, label in (("mes", "MES H60"), ("mgc", "MGC H60"), ("mnqcont", "MNQ Continuation"),
                        ("mes2r", "MES Tail (2R)")):
        g_leg = (snapshot.get("dashboard_extras") or {}).get(f"{key}_leg")
        if g_leg and g_leg.get("unrealized_pnl_usd") is not None:
            rows.append((label, g_leg["unrealized_pnl_usd"]))
    if not rows:
        return
    cols = st.columns(len(rows))
    for col, (label, val) in zip(cols, rows):
        col.metric(f"{label} — unrealized", money(val))
    st.caption("Mark-to-market off the last received bar, not a fill -- no commission or slippage included, and it moves every minute the market does.")


_CANDLESTICK_LEGS = [("t1_leg", "T1 / MNQ"), ("mnqcont_leg", "MNQ Continuation"),
                     ("mes_leg", "MES 1R"), ("mgc_leg", "MGC 1R")]
_ENTRY_COLOR, _TARGET_COLOR, _STOP_COLOR = "#9aa4b2", "#3ecf8e", "#e34948"


def _candlestick_chart(bars: pd.DataFrame, entry_fill: float | None, direction: int | None,
                       stop_price: float | None = None, target_price: float | None = None) -> alt.Chart:
    """Classic OHLC candlestick: mark_rule for the high-low wick, mark_bar for the open-close
    body, colored by direction (blue = closed up, red = closed down -- matches the diverging
    pair already used elsewhere on this dashboard, e.g. the daily P&L calendar). When a position
    is open: a gray dashed entry line, plus green/red dashed target/stop lines when the leg's
    bracket is a known fixed R-multiple (T1 has no such fixed bracket, so these stay None there)."""
    up_color, down_color = "#4f8fd6", "#e34948"
    base = alt.Chart(bars).encode(
        x=alt.X("ts:T", title=None, axis=alt.Axis(grid=False, format="%H:%M")),
    )
    color = alt.condition("datum.close >= datum.open", alt.value(up_color), alt.value(down_color))
    wick = base.mark_rule(strokeWidth=1).encode(
        y=alt.Y("low:Q", title="Price", scale=alt.Scale(zero=False)), y2="high:Q", color=color,
    )
    body = base.mark_bar(size=3).encode(
        y="open:Q", y2="close:Q", color=color,
        tooltip=[alt.Tooltip("ts:T", title="Time"), alt.Tooltip("open:Q", title="Open", format=",.2f"),
                 alt.Tooltip("high:Q", title="High", format=",.2f"), alt.Tooltip("low:Q", title="Low", format=",.2f"),
                 alt.Tooltip("close:Q", title="Close", format=",.2f")],
    )
    layers = [wick, body]

    def _level(y: float, color: str) -> alt.Chart:
        return alt.Chart(pd.DataFrame({"y": [y]})).mark_rule(strokeDash=[4, 3], strokeWidth=1.5, color=color).encode(y="y:Q")

    if entry_fill is not None:
        layers.append(_level(entry_fill, _ENTRY_COLOR))
    if target_price is not None:
        layers.append(_level(target_price, _TARGET_COLOR))
    if stop_price is not None:
        layers.append(_level(stop_price, _STOP_COLOR))
    return alt.layer(*layers).properties(height=220).configure_view(strokeWidth=0)


def render_candlesticks(snapshot: dict[str, Any]) -> None:
    """Trailing-window candlestick view per leg (dashboard_extras.{key}.ohlc_bars), with the open
    position's entry/stop/target overlaid as dashed rules when one exists. Legs with no bars yet
    (not live, or no recent data) are skipped entirely rather than shown empty."""
    extras = snapshot.get("dashboard_extras") or {}
    panels = []
    for key, label in _CANDLESTICK_LEGS:
        leg = extras.get(key)
        bars = (leg or {}).get("ohlc_bars") or []
        if len(bars) < 2:
            continue
        panels.append((key, label, leg, bars))
    if not panels:
        return
    st.subheader("Live session")
    cols = st.columns(2)
    for i, (key, label, leg, bars) in enumerate(panels):
        with cols[i % 2]:
            df = pd.DataFrame(bars)
            df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
            unrealized = leg.get("unrealized_pnl_usd") if key != "t1_leg" else snapshot["status"].get("unrealized_pnl_usd")
            title = label
            if unrealized is not None:
                title += f" — unrealized {money(unrealized)}"
            elif leg.get("phase") == "EXITED" and leg.get("total_pnl_usd") is not None:
                # Real gap, 2026-10-20: a leg's realized session P&L (dashboard_extras.{key}.total_pnl_usd)
                # was computed and published but never actually shown anywhere on the site once the
                # position closed -- unrealized goes back to None on exit, so the caption just went
                # blank right at the moment there was finally a closed trade worth showing.
                title += f" — closed {money(leg['total_pnl_usd'])}"
            st.caption(title)
            st.altair_chart(_candlestick_chart(df, leg.get("overlay_entry_fill"), leg.get("overlay_direction"),
                                               leg.get("overlay_stop_price"), leg.get("overlay_target_price")),
                            width="stretch")
    st.caption("Last 2 hours, 1-minute bars. Dashed lines mark the open position's entry (gray), target (green) "
               "and stop (red), when one exists.")


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

    st.subheader("Daily P&L")
    daily = _resampled_cum_pnl("1D", "date")
    st.altair_chart(_area_chart(daily, "date", "cumulative_pnl_usd", "Date", "Cumulative P&L ($)"), width="stretch")

    st.subheader("Drawdown")
    dd = curve[["ts_utc", "drawdown_usd"]].copy()
    st.altair_chart(_area_chart(dd, "ts_utc", "drawdown_usd", "Time", "Drawdown ($)", fill_color="#e3494833", line_color="#e34948"), width="stretch")


def _area_chart(df: pd.DataFrame, x_col: str, y_col: str, x_label: str, y_label: str,
                 fill_color: str = "#4f8fd633", line_color: str = "#4f8fd6") -> alt.Chart:
    """Gradient-filled line chart matching the dashboard's dark theme -- smoother and more
    polished than st.line_chart's bare default, same data, no new dependency (Altair is
    already used by the P&L calendar heatmap below)."""
    base = alt.Chart(df).encode(
        x=alt.X(f"{x_col}:T", title=x_label, axis=alt.Axis(grid=True, gridOpacity=0.08, gridColor="#ffffff")),
        y=alt.Y(f"{y_col}:Q", title=y_label, axis=alt.Axis(grid=True, gridOpacity=0.08, gridColor="#ffffff")),
        tooltip=[alt.Tooltip(f"{x_col}:T", title=x_label), alt.Tooltip(f"{y_col}:Q", title=y_label, format="$,.2f")],
    )
    area = base.mark_area(interpolate="monotone", line=False,
                           color=alt.Gradient(gradient="linear",
                                               stops=[alt.GradientStop(color=fill_color, offset=0),
                                                      alt.GradientStop(color="#ffffff00", offset=1)],
                                               x1=1, x2=1, y1=1, y2=0))
    line = base.mark_line(interpolate="monotone", color=line_color, strokeWidth=2)
    return (area + line).properties(height=260).configure_view(strokeWidth=0)


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
    running right now, separate from the closed-trade ledger. A leg running in SHADOW (OBSERVE, no real orders)
    is labeled explicitly rather than looking like a live position. Absent entirely on an older snapshot or if
    this leg's status file isn't present.

    6J/LE/PA were disabled 2026-10-15, and EIA/ON-001/ES dropped 2026-10-16 (not robust enough to keep
    running alongside the stronger legs) -- all intentionally excluded from `labels`. MGC10 and M6E
    both retired 2026-10-27 -- same treatment. If the underlying
    snapshot still carries a stale entry for one of them, it falls through to the generic `key` fallback
    below rather than being actively displayed with a maintained label.

    wheel_v1_mnq/wheel_v1_mgc (added 2026-10-27): Wheel v1.0's own real execution legs -- `order_qty`
    here is actually the HELD qty (these legs resize an existing position rather than enter fresh each
    trigger; see export_t1_private_snapshot.py's own _build_contract_distribution comment)."""
    dist = (snapshot.get("dashboard_extras") or {}).get("contract_distribution")
    if not dist or not dist.get("legs"):
        return
    st.subheader("Current contract distribution")
    labels = {"t1": "T1 / MNQ", "mes": "MES H60", "mgc": "MGC H60", "mnqcont": "MNQ Continuation",
              "mes2r": "MES Tail (2R)", "wheel_v1_mnq": "Wheel v1.0 MNQ", "wheel_v1_mgc": "Wheel v1.0 MGC"}
    legs = {k: v for k, v in dist["legs"].items() if k not in ("6j", "le", "pa", "on001", "eia_ho", "eia_rb", "mgc10", "m6e")}
    if not legs:
        return
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
    st.caption(f"As of {format_pst(dist['as_of_utc'])}. Reflects live order size right now, not the size any past trade in the ledger below was taken at. SHADOW = tracked for research, no real orders placed.")


def render_ops_health(snapshot: dict[str, Any]) -> None:
    """Deterministic ops health for the 7 watchdog-managed legs (dashboard_extras.ops_health,
    written by live/scripts/ops_health_check.py). This is operational health only -- process
    alive, stream/heartbeat freshness, broker-reconciliation flags, authorization expiry -- not
    a read on whether any strategy is still statistically working (that's edge health, tracked
    separately and not published here). Absent entirely on an older snapshot."""
    ops = (snapshot.get("dashboard_extras") or {}).get("ops_health")
    if not ops or not ops.get("legs"):
        return
    st.subheader("Process health")
    icons = {"HEALTHY": "\U0001F7E2", "WARNING": "\U0001F7E1", "DEGRADED": "\U0001F7E0",
             "HALTED": "\U0001F534", "UNKNOWN": "⚫"}
    overall = ops.get("overall_state", "UNKNOWN")
    st.markdown(f"**Fleet: {icons.get(overall, '')} {overall}**")
    labels = {"t1": "T1 / MNQ", "mnqcont": "MNQ Continuation", "mes": "MES H60", "mgc": "MGC H60",
              "mes2r": "MES Tail (2R)"}
    rows = []
    for key, leg in ops["legs"].items():
        auth = leg.get("auth") or {}
        days_left = auth.get("days_remaining")
        rows.append({
            "Leg": labels.get(key, key),
            "State": f"{icons.get(leg.get('state'), '')} {leg.get('state')}",
            "Stream": leg.get("stream_health") or "—",
            "Position": leg.get("position_state") or "—",
            "Auth": f"{days_left}d left" if days_left is not None else "—",
            "Notes": ", ".join(leg.get("reasons") or []) or "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption(f"Operational health only, not strategy performance. As of {format_pst(ops['generated_at'])}.")


def render_projection(snapshot: dict[str, Any]) -> None:
    """Horizon-matched risk curve for Wheel v1.0 (4 sleeves: T1, MNQCONT, MNQ, MGC; D2 reversible
    leverage ladder -> permanent switch to C30-B's rule the first time equity reaches $500,000,
    never reverts), written by scripts/project_wheel_v1_horizon_risk.py. Replaces the retired
    6-leg, P2-ladder-governed panel (MES/MGC10/MES2R dropped from "the wheel" grouping -- those
    legs keep running independently, just no longer part of this projection; see
    ~/.claude/plans/groovy-watching-dewdrop.md). Simulates the actual dynamic equity path (ladder
    level/derisk state recomputed from the simulated equity every day, the same formula the live
    wheel_v1_mnq/wheel_v1_mgc legs and shadow tracker use) via block-bootstrap, not a static
    multiple."""
    hr = (snapshot.get("dashboard_extras") or {}).get("horizon_risk")
    if not hr:
        return
    horizons = hr.get("horizons", [])
    if not horizons:
        return
    st.subheader("Drawdown risk vs. account growth, by horizon (Wheel v1.0, 4 sleeves)")
    st.caption(f"Block-bootstrap simulation ({hr.get('n_paths', '—')} paths) of the real Wheel v1.0 "
               f"D2 ladder -> C30-B derisk policy, starting from ${hr.get('start_equity_usd', 0):,.0f}. "
               f"Not a live realized track record.")
    st.caption(f"**{hr.get('policy', '')}**")

    table = pd.DataFrame([{
        "Months": h["months"],
        "Median profit ($)": h["median_pnl_usd"],
        "Median equity ($)": h["median_equity_usd"],
        "MDD typical, P50 ($)": h["mdd_p50_usd"],
        "MDD typical, P50 (% of terminal equity)": h["mdd_p50_pct_of_terminal_equity"],
        "MDD bad, P90 ($)": h["mdd_p90_usd"],
        "MDD very bad, P95 ($)": h["mdd_p95_usd"],
        "MDD very bad, P95 (% of terminal equity)": h["mdd_p95_pct_of_terminal_equity"],
        "Derisked by this horizon (%)": h.get("pct_paths_derisked", 0.0),
    } for h in horizons])
    column_config = {
        "Months": st.column_config.NumberColumn(format="%d"),
        "Median profit ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "Median equity ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "MDD typical, P50 ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "MDD typical, P50 (% of terminal equity)": st.column_config.NumberColumn(format="%.1f%%"),
        "MDD bad, P90 ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "MDD very bad, P95 ($)": st.column_config.NumberColumn(format="$%,.0f"),
        "MDD very bad, P95 (% of terminal equity)": st.column_config.NumberColumn(format="%.1f%%"),
        "Derisked by this horizon (%)": st.column_config.NumberColumn(format="%.1f%%"),
    }
    st.dataframe(table, hide_index=True, width="stretch", column_config=column_config)

    st.caption("This is the 'getting safer as we scale' read: dollar drawdowns grow as the ladder scales up with "
               "equity, but shrink as a SHARE of the account once it's grown enough to absorb them. Profit is the "
               "other side of the same simulated paths. 'Derisked by this horizon' is the share of simulated paths "
               "that had already hit the permanent $500k switch to C30-B by that point -- expect it to climb from "
               "0% toward a majority over the first few years, never back down (the switch is one-way).")
    st.caption(hr.get("caveat", ""))


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
    render_candlesticks(snapshot)
    render_metrics(snapshot)
    render_contract_distribution(snapshot)
    render_ops_health(snapshot)
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
