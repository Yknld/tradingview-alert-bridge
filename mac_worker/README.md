# Mac Worker

Local execution worker for Tradovate browser automation.

This worker is meant to run on your Mac continuously while Railway acts as the control plane.

## Phase 1 goal

- Poll Railway for the next execution job
- Open or reuse a persistent browser session
- Navigate to Tradovate
- Run in `DRY_RUN=true` first
- Log exactly what it would do

## Environment

Copy `.env.example` from this folder and fill in:

- `RAILWAY_BASE_URL`
- `WORKER_ID`
- `TRADOVATE_URL`
- `PLAYWRIGHT_USER_DATA_DIR`
- `DRY_RUN`
- `TRADOVATE_USERNAME`
- `TRADOVATE_PASSWORD`

## Install

```bash
cd /Users/danielntumba/Desktop/markov/tradingview_alert_bridge/mac_worker
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
cd /Users/danielntumba/Desktop/markov/tradingview_alert_bridge/mac_worker
. .venv/bin/activate
python worker.py
```

## First manual tests

1. Start Railway bridge
2. Create a manual execution job with `POST /execution/jobs`
3. Start this worker with `DRY_RUN=true`
4. Confirm it:
   - claims the job
   - opens Tradovate
   - logs the planned trade
   - marks the job completed

## Next steps after dry run

1. find Trading button selector
2. search symbol
3. choose buy/sell
4. set quantity
5. send market order
6. place protective stop
