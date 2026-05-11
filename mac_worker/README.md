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

## Coordinate calibration

If a Tradovate control is visually present but still resists selectors, use the click probe:

```bash
cd /Users/danielntumba/Desktop/markov/tradingview_alert_bridge/mac_worker
. .venv/bin/activate
python calibrate_clicks.py
```

Then click the live control once. The script will print the click coordinates and target metadata in the terminal.

## First manual tests

1. Start Railway bridge
2. Create a manual execution job with `POST /execution/jobs`
3. Start this worker with `DRY_RUN=true`
4. Confirm it:
   - claims the job
   - opens Tradovate
   - logs the planned trade
   - marks the job completed

## macOS permissions

If native keyboard fallback is used for stubborn Tradovate controls, macOS may require Accessibility permission for your terminal app or Codex/Chromium automation path under `System Settings > Privacy & Security > Accessibility`.

## Next steps after dry run

1. find Trading button selector
2. search symbol
3. choose buy/sell
4. set quantity
5. send market order
6. place protective stop

## Mouse overlay

To get live screen coordinates in the top-right corner while hovering Tradovate controls:

```bash
cd /Users/danielntumba/Desktop/markov/tradingview_alert_bridge/mac_worker
. .venv/bin/activate
python mouse_overlay.py
```

The HUD shows `screen_x`, `screen_y_top`, and `screen_y_bottom`.
