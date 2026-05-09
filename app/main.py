import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from twilio.rest import Client

load_dotenv()


app = FastAPI(title="TradingView Alert Bridge")
last_alert: dict[str, Any] | None = None
last_delivery_by_key: dict[str, datetime] = {}
jobs_by_id: dict[str, dict[str, Any]] = {}

PRODUCT_SPECS: dict[str, dict[str, float]] = {
    "MES": {"point_value": 5.0, "tick_value": 1.25, "tick_size": 0.25},
    "MNQ": {"point_value": 2.0, "tick_value": 0.50, "tick_size": 0.25},
    "M2K": {"point_value": 5.0, "tick_value": 0.50, "tick_size": 0.10},
    "MCL": {"point_value": 100.0, "tick_value": 1.0, "tick_size": 0.01},
    "SIC": {"point_value": 100.0, "tick_value": 1.0, "tick_size": 0.01},
    "MGC": {"point_value": 10.0, "tick_value": 1.0, "tick_size": 0.10},
    "MNG": {"point_value": 1000.0, "tick_value": 1.0, "tick_size": 0.001},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


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
    remaining_seconds = max(int((next_allowed_at - now_utc()).total_seconds()), 0)
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


def normalize_symbol_root(symbol: str) -> str:
    cleaned = symbol.upper().replace("!", "")
    matches = [root for root in PRODUCT_SPECS if cleaned.startswith(root)]
    if not matches:
        raise HTTPException(status_code=400, detail=f"Unsupported symbol for sizing: {symbol}")
    return sorted(matches, key=len, reverse=True)[0]


def choose_effective_stop(side: str, entry_price: float, natural_stop_price: float, disaster_stop_price: float | None) -> float:
    side_upper = side.upper()
    if disaster_stop_price is None:
        return natural_stop_price
    if side_upper == "LONG":
        return max(natural_stop_price, disaster_stop_price)
    if side_upper == "SHORT":
        return min(natural_stop_price, disaster_stop_price)
    raise HTTPException(status_code=400, detail=f"Unsupported side: {side}")


def compute_execution_plan(request_payload: dict[str, Any]) -> dict[str, Any]:
    symbol = str(request_payload.get("symbol", "")).upper()
    side = str(request_payload.get("side", "")).upper()
    if side not in {"LONG", "SHORT"}:
        raise HTTPException(status_code=400, detail="side must be LONG or SHORT")

    entry_price = float(request_payload["entry_price"])
    natural_stop_price = float(request_payload["natural_stop_price"])
    disaster_stop_price_raw = request_payload.get("disaster_stop_price")
    disaster_stop_price = None if disaster_stop_price_raw in (None, "") else float(disaster_stop_price_raw)
    risk_dollars = float(request_payload["risk_dollars"])
    max_contracts = int(request_payload["max_contracts"])

    root = normalize_symbol_root(symbol)
    specs = PRODUCT_SPECS[root]
    effective_stop_price = choose_effective_stop(side, entry_price, natural_stop_price, disaster_stop_price)
    stop_distance_points = abs(entry_price - effective_stop_price)
    stop_distance_ticks = stop_distance_points / specs["tick_size"] if specs["tick_size"] > 0 else 0.0
    risk_per_contract = stop_distance_ticks * specs["tick_value"]
    if risk_per_contract <= 0:
        raise HTTPException(status_code=400, detail="Calculated risk per contract must be positive")

    qty_by_risk = int(risk_dollars // risk_per_contract)
    quantity = max(min(qty_by_risk, max_contracts), 0)

    return {
        "symbol_root": root,
        "side": side,
        "entry_price": entry_price,
        "natural_stop_price": natural_stop_price,
        "disaster_stop_price": disaster_stop_price,
        "effective_stop_price": effective_stop_price,
        "stop_distance_points": stop_distance_points,
        "stop_distance_ticks": stop_distance_ticks,
        "risk_per_contract": risk_per_contract,
        "risk_dollars": risk_dollars,
        "max_contracts": max_contracts,
        "quantity": quantity,
        "tick_size": specs["tick_size"],
        "tick_value": specs["tick_value"],
        "point_value": specs["point_value"],
    }


def create_execution_job(request_payload: dict[str, Any], source: str) -> dict[str, Any]:
    plan = compute_execution_plan(request_payload)
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "status": "pending",
        "source": source,
        "created_at": now_iso(),
        "claimed_at": None,
        "claimed_by": None,
        "completed_at": None,
        "failed_at": None,
        "failure_reason": None,
        "request": request_payload,
        "plan": plan,
    }
    jobs_by_id[job_id] = job
    return job


def maybe_enqueue_execution_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    execute = bool(payload.get("execute", False))
    required_fields = {"entry_price", "natural_stop_price", "risk_dollars", "max_contracts"}
    if not execute or not required_fields.issubset(payload.keys()):
        return None
    return create_execution_job(payload, source="tradingview_webhook")


def get_next_pending_job() -> dict[str, Any] | None:
    pending_jobs = [
        job for job in jobs_by_id.values()
        if job["status"] == "pending"
    ]
    if not pending_jobs:
        return None
    pending_jobs.sort(key=lambda job: job["created_at"])
    return pending_jobs[0]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/last-alert")
def get_last_alert() -> dict[str, Any]:
    return {"last_alert": last_alert}


@app.get("/product-specs")
def get_product_specs() -> dict[str, Any]:
    return {"product_specs": PRODUCT_SPECS}


@app.post("/execution/prepare")
def prepare_execution(request_payload: dict[str, Any]) -> dict[str, Any]:
    plan = compute_execution_plan(request_payload)
    return {"ok": True, "plan": plan}


@app.post("/execution/jobs")
def create_execution_job_endpoint(request_payload: dict[str, Any]) -> dict[str, Any]:
    job = create_execution_job(request_payload, source="manual_api")
    return {"ok": True, "job": job}


@app.get("/execution/jobs")
def list_execution_jobs() -> dict[str, Any]:
    jobs = sorted(jobs_by_id.values(), key=lambda job: job["created_at"], reverse=True)
    return {"jobs": jobs}


@app.get("/execution/jobs/next")
def claim_next_execution_job(worker_id: str = Query(...)) -> dict[str, Any]:
    job = get_next_pending_job()
    if job is None:
        return {"job": None}
    job["status"] = "claimed"
    job["claimed_by"] = worker_id
    job["claimed_at"] = now_iso()
    return {"job": job}


@app.post("/execution/jobs/{job_id}/complete")
def complete_execution_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = jobs_by_id.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job["status"] = "completed"
    job["completed_at"] = now_iso()
    job["result"] = payload
    return {"ok": True, "job": job}


@app.post("/execution/jobs/{job_id}/fail")
def fail_execution_job(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    job = jobs_by_id.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job["status"] = "failed"
    job["failed_at"] = now_iso()
    job["failure_reason"] = payload.get("reason", "unknown failure")
    job["result"] = payload
    return {"ok": True, "job": job}


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
        last_delivery_by_key[cooldown["key"]] = now_utc()

    execution_job = maybe_enqueue_execution_job(payload)
    last_alert = {
        "payload": payload,
        "message": message,
        "suppressed": suppressed,
        "cooldown": cooldown,
        "delivery": delivery,
        "execution_job": execution_job,
    }
    print("TradingView alert received:", last_alert, flush=True)
    return {
        "ok": True,
        "message": message,
        "suppressed": suppressed,
        "cooldown": cooldown,
        "delivery": delivery,
        "execution_job": execution_job,
    }
