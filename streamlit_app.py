from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
    st.line_chart(hourly, x="hour", y="cumulative_pnl_usd", x_label="Hour (UTC)", y_label="Cumulative P&L ($)", height=280)

    st.subheader("Daily P&L")
    daily = _resampled_cum_pnl("1D", "date")
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
    table = trades[[
        "closed_at_utc", "mode", "side", "exit_reason", "duration_min", "pnl_usd", "expected_net_pnl_model_usd",
        "entry_rss_mb", "entry_process_cpu_pct", "entry_host_cpu_user_pct", "entry_host_load1",
    ]].copy()
    table["closed_at_utc"] = pd.to_datetime(table["closed_at_utc"], utc=True)
    st.caption(
        "\"Model\" is the frozen backtest's per-trade edge assumption, not a per-trade prediction -- "
        "same number on every row, there to compare against realized P&L, not to be read as a forecast. "
        "RSS/CPU/load columns are a snapshot of this box at the exact bar RT1 decided to enter that trade."
    )
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
            "expected_net_pnl_model_usd": st.column_config.NumberColumn("Model P&L", format="$%.2f"),
            "entry_rss_mb": st.column_config.NumberColumn("RT1 RSS", format="%.0f MB"),
            "entry_process_cpu_pct": st.column_config.NumberColumn("RT1 CPU", format="%.0f%%"),
            "entry_host_cpu_user_pct": st.column_config.NumberColumn("Host CPU", format="%.0f%%"),
            "entry_host_load1": st.column_config.NumberColumn("Load (1m)", format="%.2f"),
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
    cols[4].metric("Mean reference-to-fill shortfall", f"{t['slippage_ticks_mean']:.2f} ticks" if t["slippage_ticks_mean"] is not None else "—")
    cols[5].metric("p95 reference-to-fill shortfall", f"{t['slippage_ticks_p95']:.2f} ticks" if t["slippage_ticks_p95"] is not None else "—")
    caption = (
        "\"Reference-to-fill shortfall\" (not \"slippage\"): the gap between RT1's own theoretical "
        "signal/exit price and the real fill. This bundles multiple things -- market movement before "
        "submit, actual broker/BBO execution cost, and (for exits specifically) a strategy-semantics "
        "component from reacting only at bar-close rather than an intrabar touch. It does NOT mean "
        "Tradovate cost this many ticks -- a decomposed breakdown is planned once enough trades "
        "accumulate."
    )
    if t["sample_size"] < 30:
        caption += f" Only {t['sample_size']} sample(s) so far -- treat as directional only, not a stable mean."
    st.caption(caption)


def render_pnl_waterfall(snapshot: dict[str, Any]) -> None:
    """Backtest -> Live Bridge panel #1: where did the model's P&L go on
    the way to a real fill? Model P&L -> A (semantic/reference gap) -> B
    (decision->submit movement) -> C (execution residual) -> Fees ->
    Realized, both aggregated and per-trade."""
    wf = snapshot["pnl_waterfall"]
    st.subheader("Backtest → Live: P&L waterfall")
    if wf["n_trades"] == 0:
        st.info("No decomposed trades yet -- this fills in once real fills accumulate and the offline A/B/C decomposition has run for them.")
        return

    agg = wf["aggregate"]
    st.caption(f"n={wf['n_trades']} decomposed trade(s), mean $/trade:")
    cols = st.columns(6)
    cols[0].metric("Model P&L", money(agg["model_expectancy_usd"]))
    cols[1].metric("A: semantic/reference", money(agg["a_expectancy_usd"]))
    cols[2].metric("B: decision→submit", money(agg["b_expectancy_usd"]))
    cols[3].metric("C: execution residual", money(agg["c_expectancy_usd"]))
    cols[4].metric("Fees", money(agg["fees_expectancy_usd"]))
    cols[5].metric("Realized P&L", money(agg["realized_expectancy_usd"]))
    st.caption(
        "Model P&L is what the frozen backtest's own bar-close prices would have earned. A/B/C/Fees "
        "are subtracted from it to reach Realized -- A is the gap between RT1's theoretical reference "
        "price and the real market at decision time, B is market movement between deciding and "
        "submitting, C is the actual broker/spread execution cost. If Model ≈ Realized, execution "
        "isn't the problem; if they diverge, this shows exactly which layer (A, B, or C) is responsible."
    )
    if wf["n_missing_decomposition"]:
        st.caption(f"{wf['n_missing_decomposition']} real trade(s) still missing decomposition coverage (incomplete quote data) -- excluded above, not silently zeroed.")

    df = pd.DataFrame(wf["trades"])
    st.dataframe(
        df,
        hide_index=True,
        use_container_width=True,
        column_order=["closed_at_utc", "side", "exit_reason", "model_pnl_usd", "a_usd", "b_usd", "c_usd", "fees_usd", "realized_pnl_usd"],
        column_config={
            "closed_at_utc": st.column_config.DatetimeColumn("Closed"),
            "side": "Side",
            "exit_reason": "Exit",
            "model_pnl_usd": st.column_config.NumberColumn("Model", format="$%.2f"),
            "a_usd": st.column_config.NumberColumn("A", format="$%.2f"),
            "b_usd": st.column_config.NumberColumn("B", format="$%.2f"),
            "c_usd": st.column_config.NumberColumn("C", format="$%.2f"),
            "fees_usd": st.column_config.NumberColumn("Fees", format="$%.2f"),
            "realized_pnl_usd": st.column_config.NumberColumn("Realized", format="$%.2f"),
        },
    )


_LATENCY_STAGE_LABELS = {
    "queue_wait_ms": "Queue wait",
    "ingest_and_decision_ms": "Ingest + decision",
    "get_account_http_ms": "get_account (HTTP)",
    "submit_order_ms": "submit_order",
    "poll_until_filled_ms": "Fill polling",
    "post_entry_reconciliation_ms": "Post-entry reconciliation",
    "total_bar_to_submit_ms": "Total (bar → submit)",
}


def render_latency_health(snapshot: dict[str, Any]) -> None:
    lh = snapshot["latency_health"]
    st.subheader("Bar-to-order latency")
    if lh["verdict"] == "INSUFFICIENT_DATA":
        st.info("No bar_timing.jsonl data yet.")
        return

    verdict_fn = {"HEALTHY": st.success, "MARGINAL": st.warning, "NEEDS_UPGRADE": st.error}[lh["verdict"]]
    verdict_fn(
        f"{lh['verdict']} -- {lh['bars_sampled']:,} bars sampled, "
        f"{lh['stalls_over_750ms']} stall(s) > 750ms, {lh['stalls_over_2s']} stall(s) > 2s."
    )

    cols = st.columns(2)
    cols[0].metric("Host CPU (median, user%)", f"{lh['cpu_median_pct']:.0f}%" if lh["cpu_median_pct"] is not None else "—")
    cols[1].metric("Load average 1m (median)", f"{lh['load1_median']:.2f}" if lh["load1_median"] is not None else "—")
    st.caption(
        "Falsification check for a multi-second stall: host CPU/load spiking at the same "
        "timestamp means contention is still plausible; a clean host means the delay is "
        "inside one of the stages below (ingest/decision, get_account, submit, fill "
        "polling, or reconciliation)."
    )

    for key, label in _LATENCY_STAGE_LABELS.items():
        s = lh["stages"][key]
        if not s["n"]:
            st.write(f"**{label}**: no samples")
            continue
        st.write(
            f"**{label}** (n={s['n']}): median={s['median_ms']:.1f}ms "
            f"p95={s['p95_ms']:.1f}ms p99={s['p99_ms']:.1f}ms"
        )


def render_warmup_replay(snapshot: dict[str, Any]) -> None:
    wr = snapshot["warmup_replay"]
    st.subheader("Warmup / replay (diagnostic only)")
    st.caption(
        "Every restart re-requests a Databento replay window; once the market's open that window can "
        "overlap already-processed minutes, which come back as a fast burst. This section exists so that "
        "burst's queue backlog stays observable without ever mixing into the live latency numbers above -- "
        "a 2026-08-16 incident let exactly this backlog (up to 1.4s) masquerade as a live queue stall."
    )
    if not wr["bars_replayed"]:
        st.info("No replay burst on this run (or it hasn't happened yet).")
        return
    cols = st.columns(3)
    cols[0].metric("Bars replayed", f"{wr['bars_replayed']:,}")
    cols[1].metric("Elapsed", f"{wr['elapsed_wall_seconds']:.1f}s" if wr["elapsed_wall_seconds"] is not None else "—")
    cols[2].metric("Max queue wait", f"{wr['queue_wait_max_ms']:.0f}ms" if wr["queue_wait_max_ms"] is not None else "—")


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


_GUARDIAN_STATE_ICON = {
    "BOOT": "⚪", "RECONCILING": "⚪", "WARMUP": "⚪",
    "ARMED": "🟢", "IN_POSITION": "🔵",
    "EXIT_PENDING": "🟠", "VERIFY_FLAT": "🟠",
    "LOCKED": "🔴", "EMERGENCY": "🔴",
}


def render_guardian_transitions(snapshot: dict[str, Any]) -> None:
    transitions = snapshot["guardian_transitions"]
    st.subheader("Guardian state timeline")
    if not transitions:
        st.info("No state transitions logged yet.")
        return
    for t in transitions:
        icon = _GUARDIAN_STATE_ICON.get(t["to_state"], "⚪")
        st.write(f"{icon} `{t['ts_utc']}` **{t['from_state']}** → **{t['to_state']}** -- {t['reason']}")


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
    render_pnl_waterfall(snapshot)
    render_execution_telemetry(snapshot)
    render_latency_health(snapshot)
    render_warmup_replay(snapshot)
    render_reliability(snapshot)
    render_guardian_transitions(snapshot)


st.title("watch joey lose money")
st.caption("An unnecessarily sophisticated system for losing $0.50 at a time.")

live_dashboard()
