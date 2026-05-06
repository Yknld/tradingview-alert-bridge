import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI
from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()


app = FastAPI(title="TradingView Alert Bridge")
last_alert: dict[str, Any] | None = None
last_delivery_by_key: dict[str, datetime] = {}


def build_message(payload: dict[str, Any]) -> str:
    symbol = payload.get("symbol", "unknown symbol")
    side = payload.get("side", "unknown side")
    price = payload.get("price", "unknown price")
    timeframe = payload.get("timeframe", "unknown timeframe")
    strategy = payload.get("strategy", "strategy")
    event = payload.get("event", "ALERT")
    trigger_price = payload.get("trigger_price")
    if trigger_price not in (None, ""):
        return f"{strategy}. {event}. {symbol}. {side}. Price {price}. Trigger {trigger_price}. Timeframe {timeframe}."
    return f"{strategy}. {event}. {symbol}. {side}. Price {price}. Timeframe {timeframe}."


def get_cooldown_minutes() -> int:
    raw = os.getenv("ALERT_COOLDOWN_MINUTES", "30")
    try:
        return max(int(raw), 0)
    except ValueError:
        return 30


def build_cooldown_key(payload: dict[str, Any]) -> str:
    symbol = str(payload.get("symbol", "unknown symbol")).upper()
    side = str(payload.get("side", "unknown side")).upper()
    notify = str(payload.get("notify", "call_sms")).upper()
    event = str(payload.get("event", "ALERT")).upper()
    return f"{symbol}|{side}|{notify}|{event}"


def get_cooldown_status(payload: dict[str, Any]) -> dict[str, Any]:
    cooldown_minutes = get_cooldown_minutes()
    if cooldown_minutes <= 0:
        return {
            "allowed": True,
            "key": build_cooldown_key(payload),
            "cooldown_minutes": cooldown_minutes,
            "remaining_seconds": 0,
        }

    key = build_cooldown_key(payload)
    now = datetime.now(timezone.utc)
    last_sent_at = last_delivery_by_key.get(key)
    if last_sent_at is None:
        return {
            "allowed": True,
            "key": key,
            "cooldown_minutes": cooldown_minutes,
            "remaining_seconds": 0,
        }

    cooldown_window = timedelta(minutes=cooldown_minutes)
    next_allowed_at = last_sent_at + cooldown_window
    remaining_seconds = max(int((next_allowed_at - now).total_seconds()), 0)
    return {
        "allowed": remaining_seconds == 0,
        "key": key,
        "cooldown_minutes": cooldown_minutes,
        "remaining_seconds": remaining_seconds,
        "last_sent_at": last_sent_at.isoformat(),
        "next_allowed_at": next_allowed_at.isoformat(),
    }


def get_twilio_client() -> Client | None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        return None
    return Client(account_sid, auth_token)


def maybe_send_notifications(message: str, payload: dict[str, Any]) -> dict[str, str | None]:
    client = get_twilio_client()
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("ALERT_TO_NUMBER")

    if client is None or not from_number or not to_number:
        return {"mode": None, "call_sid": None, "sms_sid": None}

    notify_mode = str(payload.get("notify", "call_sms")).lower()

    if notify_mode == "sms_only":
        sms = client.messages.create(
            to=to_number,
            from_=from_number,
            body=message,
        )
        return {"mode": notify_mode, "call_sid": None, "sms_sid": sms.sid}

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
    return {"mode": notify_mode, "call_sid": call.sid, "sms_sid": sms.sid}


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
    cooldown = get_cooldown_status(payload)
    suppressed = not cooldown["allowed"]
    if suppressed:
        delivery = {"mode": payload.get("notify", "call_sms"), "call_sid": None, "sms_sid": None}
    else:
        delivery = maybe_send_notifications(message, payload)
        last_delivery_by_key[cooldown["key"]] = datetime.now(timezone.utc)
    last_alert = {
        "payload": payload,
        "message": message,
        "suppressed": suppressed,
        "cooldown": cooldown,
        "delivery": delivery,
    }
    print("TradingView alert received:", last_alert, flush=True)
    return {
        "ok": True,
        "message": message,
        "suppressed": suppressed,
        "cooldown": cooldown,
        "delivery": delivery,
    }
