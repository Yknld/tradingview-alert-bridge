import os
from typing import Any

from fastapi import FastAPI
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()


app = FastAPI(title="TradingView Alert Bridge")
last_alert: dict[str, Any] | None = None


def build_message(payload: dict[str, Any]) -> str:
    symbol = payload.get("symbol", "unknown symbol")
    side = payload.get("side", "unknown side")
    price = payload.get("price", "unknown price")
    timeframe = payload.get("timeframe", "unknown timeframe")
    strategy = payload.get("strategy", "strategy")
    return f"{strategy}. {symbol}. {side}. Price {price}. Timeframe {timeframe}."


def get_twilio_client() -> Client | None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return None
    return Client(account_sid, auth_token)


def maybe_send_call_and_sms(message: str) -> dict[str, str | None]:
    client = get_twilio_client()
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("ALERT_TO_NUMBER")

    if client is None or not from_number or not to_number:
        return {"call_sid": None, "sms_sid": None}

    call = client.calls.create(
        to=to_number,
        from_=from_number,
        twiml=f"<Response><Say voice='alice'>{message}</Say></Response>",
    )
    sms = client.messages.create(
        to=to_number,
        from_=from_number,
        body=message,
    )
    return {"call_sid": call.sid, "sms_sid": sms.sid}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/last-alert")
def get_last_alert() -> dict[str, Any]:
    return {"last_alert": last_alert}


@app.post("/webhook/tradingview")
def tradingview_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    global last_alert

    message = build_message(payload)
    delivery = maybe_send_call_and_sms(message)
    last_alert = {
        "payload": payload,
        "message": message,
        "delivery": delivery,
    }
    print("TradingView alert received:", last_alert, flush=True)
    return {
        "ok": True,
        "message": message,
        "delivery": delivery,
    }
