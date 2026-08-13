# watch joey lose money

A public, deliberately sanitized Streamlit dashboard for RT1-SESSION-REGIME's
Tradovate demo-account trading. Currently DEMO mode (no real money) —
`sanitizer.py` already implements the LIVE 15-minute embargo for when that
changes.

## Architecture

```
RT1 / Guardian / Tradovate  (private, never exposed)
        |
        v  read-only
trade_journal.jsonl + ledger
        |
        v  mnq_rt1_live.export_private_snapshot
private_snapshot.json  (superset, NOT committed, NOT public)
        |
        v  sanitizer.py (whitelist only, closed trades only, LIVE-delayed)
public_snapshot.json  (the only file that ever leaves the trading machine)
        |
        v  git push
GitHub repo -> Streamlit Community Cloud -> watchjoeylosemoney.streamlit.app
```

Trading never waits on publishing. Nothing in this repo can submit an order,
touch the guardian's halted state, or call `resume()`.

## What's public vs private

Public: mode, generic service/guardian/market-data state, aggregate stats
(P&L, drawdown, win rate, expectancy, profit factor), a sanitized equity
curve, up to 25 delayed closed trades (side/pnl/exit_reason/duration only),
and aggregate execution-latency/slippage stats.

Never public: account IDs, order IDs, current/open position, exact entry/
stop/target prices, working orders, credentials, strategy internals, raw
Databento/Tradovate payloads. See `sanitizer.py` and
`tests/test_snapshot_schema.py` for the enforced whitelist.

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
streamlit run streamlit_app.py
```

## Publishing a new snapshot

On the trading machine (outside this repo's checkout):

```bash
cd live/
python -m mnq_rt1_live.export_private_snapshot --output runtime/private_snapshot.json
```

Then, from this repo:

```bash
python sanitizer.py \
  --input /path/to/private_snapshot.json \
  --output public_snapshot.json \
  --schema public_snapshot.schema.json \
  --live-delay-minutes 15
pytest -q
git add public_snapshot.json
git commit -m "data: publish sanitized dashboard snapshot"
git push origin main
```

A cron/systemd timer running that sequence every 5 minutes is the intended
steady state — see the publisher script pattern in the design doc this repo
was built from.

## Deploying to Streamlit Community Cloud

1. Push this repo to GitHub (private is fine to start).
2. In Streamlit Community Cloud: New app → this repo → branch `main` →
   entrypoint `streamlit_app.py` → Python 3.12.
3. Verify the deployed app renders the sanitized snapshot correctly, then
   flip sharing to public.
4. Point `watchjoeylosemoney.com` at the resulting `*.streamlit.app` URL via
   an HTTP redirect at your registrar (Community Cloud does not support
   arbitrary apex-domain hosting).

## Incident response

If a secret ever lands in this repo's history: rotate the credential first,
then use `git-filter-repo --sensitive-data-removal` to clean history. A
force-push alone does not remove it from forks, clones, or cached views.
