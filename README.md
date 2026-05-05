# TradingView Alert Bridge

Standalone webhook service for receiving TradingView alerts and escalating them to phone/SMS/push channels.

Planned flow:

1. TradingView sends a webhook to this service.
2. The service triggers phone/SMS notifications immediately.
3. You answer your phone and place the trade manually.

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
- `POST /webhook/tradingview`

## Notes

- No shared secret
- No dedupe
- No database
- Just receive the TradingView payload and call/text your phone
