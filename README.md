# TradingView Alert Bridge

Standalone webhook service for receiving TradingView alerts, escalating them to phone/SMS channels, and preparing execution jobs for a local Mac worker.

Current flow:

1. TradingView sends a webhook to this service.
2. The service triggers phone/SMS notifications.
3. Optional: the service prepares an execution job.
4. A local Mac worker can poll Railway for the next job and execute it in Tradovate.

Initial stack:

- FastAPI
- Uvicorn
- Railway deployment
- Twilio integration

## Structure

- `app/main.py`: FastAPI entrypoint
- `.env.example`: expected environment variables
- `requirements.txt`: Python dependencies
- `railway.json`: simple Railway start config

## Endpoints

- `GET /health`
- `GET /last-alert`
- `GET /product-specs`
- `POST /execution/prepare`
- `POST /execution/jobs`
- `GET /execution/jobs`
- `GET /execution/jobs/next?worker_id=...`
- `POST /execution/jobs/{job_id}/complete`
- `POST /execution/jobs/{job_id}/fail`
- `POST /webhook/tradingview`

## Notes

- No shared secret
- No database yet
- Jobs are currently stored in memory
- Notifications and execution are now separate concerns

## Execution plan rules

For an execution request, the service:

1. detects the product spec from the symbol root
2. compares:
   - natural stop
   - disaster stop
3. chooses the tighter effective stop:
   - long: `max(natural_stop, disaster_stop)`
   - short: `min(natural_stop, disaster_stop)`
4. computes quantity from:
   - risk dollars
   - tick value
   - tick size
   - max contracts

## Example execution prepare payload

```json
{
  "symbol": "MNQM6",
  "side": "LONG",
  "entry_price": 29335.5,
  "natural_stop_price": 29285.5,
  "disaster_stop_price": 29300.5,
  "risk_dollars": 400,
  "max_contracts": 10
}
```
