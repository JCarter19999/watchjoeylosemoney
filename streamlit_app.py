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


def render_normality_panel(snapshot: dict[str, Any]) -> None:
    """"Is G1 behaving normally?" -- live stats compared against the
    historical distribution of same-SIZE trade blocks (not the full 66k-trade
    aggregate), so this is a real percentile, not apples vs oranges."""
    extras = snapshot.get("dashboard_extras") or {}
    norm = extras.get("normality")
    st.subheader("Is G1 behaving normally?")
    if not norm:
        st.info("Not enough closed trades yet to compare against history.")
        return

    n = norm["n_trades"]
    ref_n = norm.get("reference_n")
    scale = norm.get("reference_scale_applied")
    scale_note = f" Dollar figures rescaled {scale:.2f}x to match current live sizing." if scale and abs(scale - 1.0) > 0.01 else ""
    st.caption(
        (f"Live sample: {n} closed trade(s). Compared against every historical {ref_n}-trade block, "
         f"the EXACT same length as today's sample (overlapping windows, 2020-2026 basis, computed at "
         f"qty=3), not a nearby bucket size or the full 66,000-trade aggregate.{scale_note}") if ref_n else
        f"Live sample: {n} closed trade(s). No historical reference available yet."
    )
    cols = st.columns(3)
    cols[0].metric(
        "Expectancy", money(norm["expectancy_usd"]),
        f"{norm['expectancy_percentile']:.0f}th percentile" if "expectancy_percentile" in norm else None,
    )
    if norm.get("reference_expectancy_p50") is not None:
        cols[0].caption(f"Historical median at this sample size: {money(norm['reference_expectancy_p50'])}")
    cols[1].metric(
        "Profit factor", f"{norm['profit_factor']:.2f}" if norm["profit_factor"] is not None else "—",
        f"{norm['profit_factor_percentile']:.0f}th percentile" if "profit_factor_percentile" in norm else None,
    )
    if norm.get("reference_pf_p50") is not None:
        cols[1].caption(f"Historical median: {norm['reference_pf_p50']:.2f}")
    cols[2].metric(
        "Max drawdown (this sample)", money(norm["mdd_usd"]),
        f"{norm['mdd_percentile']:.0f}th percentile" if "mdd_percentile" in norm else None,
        delta_color="inverse",
    )
    if norm.get("reference_mdd_p50") is not None:
        cols[2].caption(
            f"Historical median {money(norm['reference_mdd_p50'])} · "
            f"Q95 {money(norm['reference_mdd_p95'])} · Q99 {money(norm['reference_mdd_p99'])}"
        )
    st.caption(
        "Percentile = where the live number falls among every historical window of the same trade "
        "count, low percentile on expectancy/PF is bad, low percentile on MDD (more negative) is also "
        "bad. This answers \"is G1 behaving like G1\", not just \"is it making money right now.\""
    )
    st.caption(
        f"Qualification progress: {n:,} / {_QUALIFICATION_TARGET_TRADES:,} trades toward the frozen "
        "forward-qualification target (max of 6 weeks or 1,500 clean trades)."
    )
    st.progress(min(1.0, n / _QUALIFICATION_TARGET_TRADES))


def render_mfe_mae_panel(snapshot: dict[str, Any]) -> None:
    """MFE/MAE in ATR units -- G1's behavioral fingerprint groundwork.
    Stayed remarkably stable (~1.7 MFE_ATR / ~1.0 MAE_ATR) across every
    year 2018-2026 including the very different low-ATR 2018-19 regime,
    which is what makes it a real signal: if P&L moves but these don't,
    that's likely a normal unfavorable realization; if these shift even
    while P&L looks fine, that's the more concerning case."""
    extras = snapshot.get("dashboard_extras") or {}
    mm = extras.get("mfe_mae")
    st.subheader("MFE / MAE fingerprint (early)")
    if not mm:
        st.info("No trades with MFE/MAE telemetry yet (added 2026-09-08 -- trades before that don't carry it).")
        return
    cols = st.columns(2)
    cols[0].metric("MFE / ATR (live avg)", f"{mm['live_mfe_atr_mean']:.2f}" if mm.get("live_mfe_atr_mean") is not None else "—",
                    f"historical {mm['historical_mfe_atr_mean']:.2f}")
    cols[1].metric("MAE / ATR (live avg)", f"{mm['live_mae_atr_mean']:.2f}" if mm.get("live_mae_atr_mean") is not None else "—",
                    f"historical {mm['historical_mae_atr_mean']:.2f}")
    st.caption(
        f"n={mm['n_trades']} trade(s) with telemetry -- far too small to read anything into a deviation yet. "
        "This is groundwork for a real comparison once enough forward trades accumulate: these ratios stayed "
        "nearly identical across every historical regime (2018-19 low-ATR through 2020-26), so a live deviation "
        "here would be a more specific signal than P&L alone that something about the mechanics changed."
    )


_REGIME_CELL_ORDER = [("high", "chop"), ("high", "mixed"), ("high", "trend"),
                      ("mid", "chop"), ("mid", "mixed"), ("mid", "trend"),
                      ("low", "chop"), ("low", "mixed"), ("low", "trend")]
_REGIME_LABELS = {"high": "High ATR", "mid": "Mid ATR", "low": "Low ATR",
                   "chop": "Choppy", "mixed": "Mixed", "trend": "Trending"}


def render_regime_panel(snapshot: dict[str, Any]) -> None:
    """ATR (opportunity size) x directional efficiency (trend vs chop) --
    two genuinely separate axes discovered 2026-09-08. Today's session is
    marked against the historical $/day grid for the same cell."""
    extras = snapshot.get("dashboard_extras") or {}
    regime = extras.get("regime")
    st.subheader("Market regime")
    st.caption(
        "Classified once per completed session (00:35 UTC), not live -- ATR percentile and directional "
        "efficiency are full-session statistics that would be noisy/misleading computed from a partial day."
    )
    if not regime:
        st.info("No regime classification logged yet (runs once daily).")
        return
    st.caption(f"Last classified session: {regime['date']}")

    today_cell = (
        {"high": "high", "mid": "mid", "low": "low"}.get(regime["atr_regime"], "mid"),
        {"trending": "trend", "choppy": "chop", "mixed": "mixed"}.get(regime["trend_regime"], "mixed"),
    )
    cols = st.columns(3)
    cols[0].metric("Session regime", f"{_REGIME_LABELS[today_cell[0]]} / {_REGIME_LABELS[today_cell[1]]}")
    cols[1].metric("Directional efficiency", f"{regime['directional_efficiency']:.4f}" if regime.get("directional_efficiency") is not None else "—",
                    help="|net move| / total churn -- near 0 is a round-trip/chop day, near 1 is a pure trend day.")
    cols[2].metric("Session range", f"{regime['session_range_pts']:.0f} pts" if regime.get("session_range_pts") is not None else "—")

    all_cells = regime.get("all_cells")
    if all_cells:
        rows = []
        for atr_b, trend_b in _REGIME_CELL_ORDER:
            key = f"{atr_b}_{trend_b}"
            cell = all_cells.get(key, {})
            is_today = (atr_b, trend_b) == today_cell
            rows.append({
                "ATR": _REGIME_LABELS[atr_b], "Persistence": _REGIME_LABELS[trend_b],
                "Median $/day (history)": cell.get("median_day_pnl"),
                "Days observed": cell.get("n_days"),
                "": "📍 LAST SESSION" if is_today else "",
            })
        df = pd.DataFrame(rows)
        st.dataframe(
            df, hide_index=True, width="stretch",
            column_config={
                "Median $/day (history)": st.column_config.NumberColumn(format="$%.0f"),
                "Days observed": st.column_config.NumberColumn(format="%d"),
            },
        )
        hist_cell = regime.get("historical_cell")
        if hist_cell:
            st.caption(
                f"Historically, {_REGIME_LABELS[today_cell[0]].lower()} + {_REGIME_LABELS[today_cell[1]].lower()} "
                f"days had a median of {money(hist_cell['median_day_pnl'])}/day across {hist_cell['n_days']} such days "
                "(2020-2026 basis) -- median rather than mean since day-level P&L is right-skewed and a few huge "
                "days would otherwise pull the average up; this is the reference to judge today's realized P&L "
                "against, not the grand average across all regimes."
            )


def render_intraday_atr_panel(snapshot: dict[str, Any]) -> None:
    """Two stacked charts sharing one time axis (ATR points, equity dollars
    -- deliberately NOT a dual-axis chart, different units don't belong on
    one scale) so a real intraday volatility compression coinciding with a
    drawdown is visible directly, instead of only inferring it after the
    fact from a single whole-session ATR average. Added 2026-09-08 after
    exactly that pattern showed up live: ATR fell from ~20 (14:00 UTC) to
    ~7 (18:00 UTC) while equity gave back over half its peak in the same
    window -- the session-level regime panel alone couldn't show this."""
    extras = snapshot.get("dashboard_extras") or {}
    atr_pts = extras.get("atr_intraday") or []
    eq_pts = extras.get("intraday_equity") or []
    st.subheader("Today's ATR vs. equity")
    if not atr_pts and not eq_pts:
        st.info("No intraday data yet today.")
        return
    st.caption(
        "Same time axis, two separate scales (points vs. dollars) -- look for a volatility compression "
        "(ATR chart flattening/falling) lining up with a drawdown (equity chart falling) below it."
    )

    if atr_pts:
        atr_df = pd.DataFrame(atr_pts)
        atr_df["ts_utc"] = pd.to_datetime(atr_df["ts_utc"], utc=True)
        st.line_chart(atr_df, x="ts_utc", y="atr", x_label="Time (your local timezone)", y_label="ATR (points)", height=220)
    else:
        st.info("No ATR history logged yet today (telemetry added 2026-09-08 -- accumulates from now on).")

    if eq_pts:
        eq_df = pd.DataFrame(eq_pts)
        eq_df["ts_utc"] = pd.to_datetime(eq_df["ts_utc"], utc=True)
        st.line_chart(eq_df, x="ts_utc", y="cum_pnl", x_label="Time (your local timezone)", y_label="Cumulative P&L today ($)", height=220)


def render_giveback_panel(snapshot: dict[str, Any]) -> None:
    """Peak-to-close giveback: how much of an intraday peak survives to the
    close. Discovered 2026-09-08 as a possibly load-bearing difference vs
    RT1 -- RT1's real live journal shows a 40% rate of days that closed
    NEGATIVE despite a real intraday peak; this tracks whether G1 differs."""
    extras = snapshot.get("dashboard_extras") or {}
    gb = extras.get("giveback")
    st.subheader("Peak-to-close giveback")
    if not gb:
        st.info("No day with a meaningful intraday peak logged yet.")
        return

    latest = gb["latest_day"]
    is_live = gb.get("latest_day_is_live", False)
    label_prefix = "Today's" if is_live else f"{latest['date']}'s (last completed session)"
    cols = st.columns(4)
    cols[0].metric(f"{label_prefix} peak", money(latest["peak"]))
    cols[1].metric("Close (so far)" if is_live else "Close", money(latest["close"]))
    cols[2].metric("Giveback", f"{latest['giveback_pct']:.1f}%" if latest.get("giveback_pct") is not None else "—")
    cols[3].metric("Sample so far", f"{gb['n_days']} day(s)")
    if is_live:
        st.caption("Updates live as trades close today -- not waiting on the once-daily regime log.")

    rt1 = gb["rt1_reference"]
    comp = pd.DataFrame([
        {"Metric": "Median giveback", "G1 (live)": f"{gb['median_giveback_pct']:.1f}%" if gb.get("median_giveback_pct") is not None else "—",
         "RT1 (live, reference)": f"{rt1['median_giveback_pct']:.1f}%"},
        {"Metric": ">100% giveback rate", "G1 (live)": pct(gb.get("over_100pct_giveback_rate")),
         "RT1 (live, reference)": pct(rt1["over_100pct_giveback_rate"])},
        {"Metric": "n days (peak > $50)", "G1 (live)": str(gb["n_days"]), "RT1 (live, reference)": str(rt1["n_days"])},
    ])
    st.dataframe(comp, hide_index=True, width="stretch")
    st.caption(
        "\">100% giveback\" means the day closed negative despite a real intraday peak -- RT1's real live "
        "journal shows this on 4 of 10 meaningful-peak days (40%). This comparison is the actual point of "
        "tracking giveback: not whether G1 wins, but whether it keeps more of what it makes. "
        f"G1's own sample ({gb['n_days']} day{'s' if gb['n_days'] != 1 else ''}) is still far too small to "
        "call this settled either way."
    )


def render_execution_panel(snapshot: dict[str, Any]) -> None:
    """Actual vs backtest-assumed execution cost, STRICTLY per-instrument --
    NQ and MNQ are never blended into one number, since their fill-capacity
    question is exactly why the mixed NQ+MNQ mapper exists in the first
    place. Purely observational data (see g1_execution_telemetry.py);
    unavailable here just means the telemetry hasn't accumulated yet, not
    that anything is wrong."""
    extras = snapshot.get("dashboard_extras") or {}
    execution = extras.get("execution")
    st.subheader("Execution: actual vs. assumed")
    if not execution:
        st.info("No completed round-trip execution telemetry yet.")
        return

    for instrument, stats in execution.items():
        st.markdown(f"**{instrument}** (n={stats['n_complete_round_trips']} complete round trips)")
        rt = stats["round_trip_slippage_ticks"]
        cols = st.columns(4)
        cols[0].metric("Backtest assumed", f"{2 * 1.0:.1f} ticks round trip")
        cols[1].metric("Live median", f"{rt.get('median'):.2f} ticks" if rt.get("median") is not None else "—")
        cols[2].metric("Live P90", f"{rt.get('p90'):.2f} ticks" if rt.get("p90") is not None else "—")
        cols[3].metric(
            "Incremental cost vs model",
            money(stats.get("incremental_cost_vs_model_usd")) + "/contract" if stats.get("incremental_cost_vs_model_usd") is not None else "—",
            delta_color="inverse",
        )
        lat = stats.get("entry_fill_latency_ms", {})
        if lat.get("n"):
            st.caption(f"Entry fill latency: median {lat['median']:.0f}ms, P90 {lat['p90']:.0f}ms (n={lat['n']}).")
    st.caption(
        "Backtest assumes 1 tick/side slippage (2 ticks round trip) plus the official commission "
        "schedule. \"Incremental cost vs model\" is the actual median round-trip cost minus that "
        "assumption, per contract -- positive means real execution is dragging more than the backtest "
        "assumed. NQ and MNQ are always shown separately: this is precisely the question the mixed "
        "execution mapper depends on getting right at scale."
    )


def render_scaling_panel(snapshot: dict[str, Any]) -> None:
    """Makes the $3,000/contract ratchet tangible without turning the page
    into a future-yacht calculator -- current tier, next tier, distance,
    and the risk-capital floor, nothing about the eventual $30M projection."""
    extras = snapshot.get("dashboard_extras") or {}
    scaling = extras.get("scaling")
    st.subheader("Scaling readiness")
    if not scaling:
        st.info("No sizer state yet.")
        return

    deployed = scaling["deployed_qty"]
    ceiling = scaling["ceiling_qty"]
    cap = scaling["max_qty_cap"]
    next_tier = min(cap, ceiling + 1)
    trigger_usd = 3000.0
    peak = scaling["peak_equity"]
    # ceiling = 3 + floor(peak/3000) -> next tier needs peak >= (next_tier-3)*3000
    distance = max(0.0, (next_tier - 3) * trigger_usd - peak) if ceiling < cap else None

    cols = st.columns(4)
    cols[0].metric("Currently deployed", f"{deployed} MNQ-eq")
    cols[1].metric("Earned ceiling", f"{ceiling} MNQ-eq" + (" (AT CAP)" if scaling.get("at_cap") else ""))
    cols[2].metric("Next tier", f"{next_tier} MNQ-eq" if ceiling < cap else "At cap")
    cols[3].metric("Distance to next tier", money(distance) if distance is not None else "—")

    if scaling.get("circuit_breaker_active"):
        st.warning(f"Q99 circuit breaker ACTIVE -- trading at base size ({deployed}) despite an earned ceiling of {ceiling}, "
                   "because current drawdown exceeds the 99th-percentile expectation for that size.")
    floor_2x = deployed * 2 * 975.05
    st.caption(
        f"Risk-capital floor at current size (2x Q99 MDD/unit): {money(floor_2x)}. "
        f"Peak realized equity: {money(peak)}. Scaling is a one-way ratchet (+1 unit per $3,000 of peak "
        "profit) with a Q99 drawdown circuit breaker that can drop to base size temporarily -- it never "
        "reduces what's been earned, only what's deployed on any single trade."
    )


def render_concentration_panel(snapshot: dict[str, Any]) -> None:
    extras = snapshot.get("dashboard_extras") or {}
    conc = extras.get("concentration")
    hist = extras.get("concentration_historical")
    st.subheader("Profit concentration")
    st.caption(
        "G1's historical distribution is right-skewed by design -- a few big trades carry a lot of the "
        "total. The question isn't whether forward P&L is concentrated, it's whether it's concentrated "
        "MORE than history predicts."
    )
    rows = []
    if hist:
        rows.append({"Basis": "Historical (2020-2026, n=66,145)", "Top 1%": pct(hist["top_1pct_contribution"]),
                     "Top 5%": pct(hist["top_5pct_contribution"]), "Top 10%": pct(hist["top_10pct_contribution"]),
                     "P&L excl. best trade": money(hist["pnl_excl_best_trade"])})
    if conc and not conc.get("insufficient_sample"):
        rows.append({"Basis": f"Live (n={conc['n_trades']})", "Top 1%": pct(conc["top_1pct_contribution"]),
                     "Top 5%": pct(conc["top_5pct_contribution"]), "Top 10%": pct(conc["top_10pct_contribution"]),
                     "P&L excl. best trade": money(conc["pnl_excl_best_trade"])})
    elif conc:
        st.info(f"Live sample (n={conc['n_trades']}) too small for a meaningful concentration read yet.")
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if conc and not conc.get("insufficient_sample") and conc["n_trades"] < 100:
        st.caption(f"At only {conc['n_trades']} live trades, 1%/5%/10% can round to the same 1-2 trades -- treat the live row as directional only.")


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
    render_normality_panel(snapshot)

    st.divider()
    render_mfe_mae_panel(snapshot)

    st.divider()
    st.subheader("Risk")
    render_charts(snapshot)
    render_intraday_atr_panel(snapshot)
    render_giveback_panel(snapshot)

    st.divider()
    render_regime_panel(snapshot)

    st.divider()
    render_execution_panel(snapshot)

    st.divider()
    render_scaling_panel(snapshot)

    st.divider()
    render_daily_pnl_heatmap(snapshot)
    render_trade_table(snapshot)
    render_reliability(snapshot)

    st.divider()
    with st.expander("Research: profit concentration"):
        render_concentration_panel(snapshot)


st.title("watch joey lose money")
st.caption("An unnecessarily sophisticated system for losing $0.50 at a time.")

live_dashboard()
