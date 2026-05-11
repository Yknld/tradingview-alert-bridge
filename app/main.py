import os
import uuid
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from twilio.rest import Client

load_dotenv()


app = FastAPI(title="TradingView Alert Bridge")
last_alert: dict[str, Any] | None = None
last_delivery_by_key: dict[str, datetime] = {}
jobs_by_id: dict[str, dict[str, Any]] = {}
positions_by_id: dict[str, dict[str, Any]] = {}
runtime_settings: dict[str, Any] = {
    "execution_enabled": os.getenv("EXECUTION_ENABLED", "true").lower() == "true",
    "max_open_positions": int(os.getenv("MAX_OPEN_POSITIONS", "2")),
    "max_open_contracts_per_account": int(os.getenv("MAX_OPEN_CONTRACTS_PER_ACCOUNT", "3")),
    "min_risk_dollars": float(os.getenv("MIN_RISK_DOLLARS", "300")),
    "max_risk_dollars": float(os.getenv("MAX_RISK_DOLLARS", "500")),
    "auto_submit_stop_loss": os.getenv("AUTO_SUBMIT_STOP_LOSS", "true").lower() == "true",
    "mirrored_account_count": int(os.getenv("MIRRORED_ACCOUNT_COUNT", "2")),
}

PRODUCT_SPECS: dict[str, dict[str, float]] = {
    "MES": {"point_value": 5.0, "tick_value": 1.25, "tick_size": 0.25},
    "MNQ": {"point_value": 2.0, "tick_value": 0.50, "tick_size": 0.25},
    "M2K": {"point_value": 5.0, "tick_value": 0.50, "tick_size": 0.10},
    "MCL": {"point_value": 100.0, "tick_value": 1.0, "tick_size": 0.01},
    "SIC": {"point_value": 100.0, "tick_value": 1.0, "tick_size": 0.01},
    "MGC": {"point_value": 10.0, "tick_value": 1.0, "tick_size": 0.10},
    "MNG": {"point_value": 1000.0, "tick_value": 1.0, "tick_size": 0.001},
}

ACTIVE_POSITION_STATUSES = {
    "pending_entry",
    "awaiting_stop",
    "protected",
    "open_unprotected",
    "stop_failed",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def get_runtime_settings() -> dict[str, Any]:
    return {
        "execution_enabled": bool(runtime_settings["execution_enabled"]),
        "max_open_positions": int(runtime_settings["max_open_positions"]),
        "max_open_contracts_per_account": max(1, int(runtime_settings["max_open_contracts_per_account"])),
        "min_risk_dollars": float(runtime_settings["min_risk_dollars"]),
        "max_risk_dollars": float(runtime_settings["max_risk_dollars"]),
        "auto_submit_stop_loss": bool(runtime_settings["auto_submit_stop_loss"]),
        "mirrored_account_count": max(1, int(runtime_settings["mirrored_account_count"])),
    }


def is_entry_job(request_payload: dict[str, Any]) -> bool:
    return str(request_payload.get("ticket_type", "entry")).lower() != "stop_loss"


def active_positions() -> list[dict[str, Any]]:
    return [
        position for position in positions_by_id.values()
        if position["status"] in ACTIVE_POSITION_STATUSES
    ]


def active_per_account_contracts() -> int:
    return sum(int(position.get("per_account_quantity", 0)) for position in active_positions())


def validate_risk_limits(request_payload: dict[str, Any]) -> None:
    settings = get_runtime_settings()
    risk_dollars = float(request_payload["risk_dollars"])
    if risk_dollars < settings["min_risk_dollars"] or risk_dollars > settings["max_risk_dollars"]:
        raise HTTPException(
            status_code=400,
            detail=(
                f"risk_dollars must be between {settings['min_risk_dollars']:.0f} "
                f"and {settings['max_risk_dollars']:.0f}"
            ),
        )


def validate_trade_capacity(request_payload: dict[str, Any], plan: dict[str, Any]) -> None:
    if not is_entry_job(request_payload):
        return
    settings = get_runtime_settings()
    active_count = len(active_positions())
    if active_count >= settings["max_open_positions"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Active trade limit reached ({active_count}/{settings['max_open_positions']}). "
                "Close a position before opening another."
            ),
        )
    current_per_account_contracts = active_per_account_contracts()
    projected_per_account_contracts = current_per_account_contracts + int(plan["per_account_quantity"])
    if projected_per_account_contracts > settings["max_open_contracts_per_account"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Open contract limit per account would be exceeded "
                f"({projected_per_account_contracts}/{settings['max_open_contracts_per_account']}). "
                "Close or reduce positions before opening another."
            ),
        )


def build_position_from_job(job: dict[str, Any]) -> dict[str, Any]:
    request_payload = job["request"]
    plan = job["plan"]
    position_id = str(uuid.uuid4())
    return {
        "position_id": position_id,
        "status": "pending_entry",
        "created_at": now_iso(),
        "opened_at": None,
        "closed_at": None,
        "close_reason": None,
        "symbol": request_payload["symbol"],
        "side": request_payload["side"],
        "quantity": plan["quantity"],
        "per_account_quantity": plan["per_account_quantity"],
        "mirrored_account_count": plan["mirrored_account_count"],
        "entry_price": plan["entry_price"],
        "effective_stop_price": plan["effective_stop_price"],
        "risk_dollars": plan["risk_dollars"],
        "total_risk_dollars": plan["total_risk_dollars"],
        "risk_per_contract": plan["risk_per_contract"],
        "source_job_id": job["job_id"],
        "stop_job_id": None,
        "entry_result": None,
        "stop_result": None,
    }


def get_position_for_job(job_id: str) -> dict[str, Any] | None:
    for position in positions_by_id.values():
        if position.get("source_job_id") == job_id or position.get("stop_job_id") == job_id:
            return position
    return None


def cancel_related_pending_jobs(position_id: str) -> None:
    position = positions_by_id[position_id]
    related_job_ids = {position.get("source_job_id"), position.get("stop_job_id")}
    for related_job_id in related_job_ids:
        if not related_job_id:
            continue
        job = jobs_by_id.get(related_job_id)
        if job is None:
            continue
        if job["status"] in {"pending", "claimed"}:
            job["status"] = "cancelled"
            job["cancelled_at"] = now_iso()
            job["failure_reason"] = "Cancelled due to manual position close"


def queue_stop_loss_job_for_position(position: dict[str, Any], entry_job: dict[str, Any]) -> dict[str, Any]:
    stop_request = dict(entry_job["request"])
    stop_request["ticket_type"] = "stop_loss"
    stop_request["linked_position_id"] = position["position_id"]
    stop_request["execute"] = True
    stop_job = create_execution_job(stop_request, source="auto_stop_loss", enforce_entry_guards=False)
    position["stop_job_id"] = stop_job["job_id"]
    position["status"] = "awaiting_stop"
    return stop_job


def render_bool_badge(value: bool, true_label: str, false_label: str) -> str:
    label = true_label if value else false_label
    color = "#15803d" if value else "#b91c1c"
    return f"<span style='color:{color};font-weight:700'>{escape(label)}</span>"


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

    notify_mode = str(payload.get("notify", "call_sms")).lower()

    if notify_mode in {"silent", "none"}:
        return {"mode": notify_mode, "call_sid": None, "sms_sid": None}

    if client is None or not from_number or not to_number:
        return {"mode": None, "call_sid": None, "sms_sid": None}

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


def parse_optional_price(raw_value: Any) -> float | None:
    if raw_value in (None, "", "0", "0.0"):
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def choose_effective_stop(side: str, entry_price: float, natural_stop_price: float | None, disaster_stop_price: float | None) -> float:
    side_upper = side.upper()
    if natural_stop_price is None and disaster_stop_price is None:
        raise HTTPException(status_code=400, detail="At least one stop price must be provided")
    if natural_stop_price is None:
        return disaster_stop_price  # type: ignore[return-value]
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
    natural_stop_price = parse_optional_price(request_payload.get("natural_stop_price"))
    disaster_stop_price = parse_optional_price(request_payload.get("disaster_stop_price"))
    risk_dollars = float(request_payload["risk_dollars"])
    max_contracts = int(request_payload["max_contracts"])
    mirrored_account_count = max(1, int(get_runtime_settings()["mirrored_account_count"]))

    root = normalize_symbol_root(symbol)
    specs = PRODUCT_SPECS[root]
    effective_stop_price = choose_effective_stop(side, entry_price, natural_stop_price, disaster_stop_price)
    if side == "LONG" and effective_stop_price >= entry_price:
        raise HTTPException(status_code=400, detail="Effective stop for LONG must be below entry price")
    if side == "SHORT" and effective_stop_price <= entry_price:
        raise HTTPException(status_code=400, detail="Effective stop for SHORT must be above entry price")
    stop_distance_points = abs(entry_price - effective_stop_price)
    stop_distance_ticks = stop_distance_points / specs["tick_size"] if specs["tick_size"] > 0 else 0.0
    risk_per_contract = stop_distance_ticks * specs["tick_value"]
    if risk_per_contract <= 0:
        raise HTTPException(status_code=400, detail="Calculated risk per contract must be positive")

    qty_by_risk = int(risk_dollars // risk_per_contract)
    per_account_quantity = max(min(qty_by_risk, max_contracts), 0)
    if per_account_quantity < 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "Risk budget is too small for even 1 contract per account at the computed stop distance. "
                "Increase risk_dollars, tighten the stop, or reduce mirrored account count."
            ),
        )
    quantity = per_account_quantity * mirrored_account_count

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
        "total_risk_dollars": risk_dollars * mirrored_account_count,
        "max_contracts": max_contracts,
        "per_account_quantity": per_account_quantity,
        "mirrored_account_count": mirrored_account_count,
        "quantity": quantity,
        "tick_size": specs["tick_size"],
        "tick_value": specs["tick_value"],
        "point_value": specs["point_value"],
    }


def create_execution_job(
    request_payload: dict[str, Any],
    source: str,
    *,
    enforce_entry_guards: bool = True,
) -> dict[str, Any]:
    if enforce_entry_guards and not runtime_settings["execution_enabled"] and is_entry_job(request_payload):
        raise HTTPException(status_code=409, detail="Execution is currently disabled")
    if enforce_entry_guards:
        validate_risk_limits(request_payload)
    plan = compute_execution_plan(request_payload)
    if enforce_entry_guards:
        validate_trade_capacity(request_payload, plan)
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
    if is_entry_job(request_payload):
        position = build_position_from_job(job)
        positions_by_id[position["position_id"]] = position
        job["position_id"] = position["position_id"]
    return job


def maybe_enqueue_execution_job(payload: dict[str, Any]) -> dict[str, Any] | None:
    execute = bool(payload.get("execute", False))
    required_fields = {"entry_price", "risk_dollars", "max_contracts"}
    if not execute or not required_fields.issubset(payload.keys()):
        return None
    if parse_optional_price(payload.get("natural_stop_price")) is None and parse_optional_price(payload.get("disaster_stop_price")) is None:
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


@app.get("/positions")
def list_positions() -> dict[str, Any]:
    positions = sorted(positions_by_id.values(), key=lambda position: position["created_at"], reverse=True)
    return {"positions": positions, "active_count": len(active_positions()), "settings": get_runtime_settings()}


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
    position = get_position_for_job(job_id)
    request_payload = job["request"]
    ticket_type = str(request_payload.get("ticket_type", "entry")).lower()

    if position is not None and ticket_type != "stop_loss":
        position["entry_result"] = payload
        mode = str(payload.get("mode", ""))
        if mode == "live_send":
            position["opened_at"] = now_iso()
            if runtime_settings["auto_submit_stop_loss"]:
                stop_job = queue_stop_loss_job_for_position(position, job)
                job["auto_stop_job"] = stop_job["job_id"]
            else:
                position["status"] = "open_unprotected"
        elif mode == "prepare_only":
            position["status"] = "staged_entry"
        else:
            position["status"] = "entry_completed"

    if position is not None and ticket_type == "stop_loss":
        position["stop_result"] = payload
        mode = str(payload.get("mode", ""))
        if mode == "live_send":
            position["status"] = "protected"
        elif mode == "prepare_only":
            position["status"] = "staged_stop"

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
    position = get_position_for_job(job_id)
    if position is not None:
        ticket_type = str(job["request"].get("ticket_type", "entry")).lower()
        if ticket_type == "stop_loss":
            position["status"] = "stop_failed"
            position["stop_result"] = payload
        else:
            position["status"] = "entry_failed"
            position["entry_result"] = payload
    return {"ok": True, "job": job}


@app.post("/positions/{position_id}/close")
def close_position(position_id: str, reason: str = Query("manual_close")) -> dict[str, Any]:
    position = positions_by_id.get(position_id)
    if position is None:
        raise HTTPException(status_code=404, detail="Position not found")
    position["status"] = "closed"
    position["closed_at"] = now_iso()
    position["close_reason"] = reason
    cancel_related_pending_jobs(position_id)
    return {"ok": True, "position": position}


def render_dashboard_html() -> str:
    settings = get_runtime_settings()
    positions = sorted(positions_by_id.values(), key=lambda position: position["created_at"], reverse=True)
    jobs = sorted(jobs_by_id.values(), key=lambda job: job["created_at"], reverse=True)[:20]
    active_count = len(active_positions())
    active_contract_count = active_per_account_contracts()

    position_rows = []
    for position in positions:
        close_button = ""
        if position["status"] in ACTIVE_POSITION_STATUSES:
            close_button = (
                f"<form method='post' action='/dashboard/positions/{escape(position['position_id'])}/close' style='display:inline'>"
                "<button type='submit'>Mark Closed</button>"
                "</form>"
            )
        position_rows.append(
            "<tr>"
            f"<td>{escape(position['position_id'][:8])}</td>"
            f"<td>{escape(position['symbol'])}</td>"
            f"<td>{escape(position['side'])}</td>"
            f"<td>{position['quantity']} total / {position['per_account_quantity']} acct</td>"
            f"<td>{position['risk_dollars']:.2f} acct / {position['total_risk_dollars']:.2f} total</td>"
            f"<td>{position['effective_stop_price']:.5f}</td>"
            f"<td>{escape(position['status'])}</td>"
            f"<td>{escape(position['created_at'])}</td>"
            f"<td>{escape(position['opened_at'] or '-')}</td>"
            f"<td>{escape(position['closed_at'] or '-')}</td>"
            f"<td>{close_button}</td>"
            "</tr>"
        )

    job_rows = []
    for job in jobs:
        job_rows.append(
            "<tr>"
            f"<td>{escape(job['job_id'][:8])}</td>"
            f"<td>{escape(str(job['request'].get('ticket_type', 'entry')))}</td>"
            f"<td>{escape(job['request'].get('symbol', ''))}</td>"
            f"<td>{escape(job['request'].get('side', ''))}</td>"
            f"<td>{escape(job['status'])}</td>"
            f"<td>{escape(str(job.get('position_id', '-'))[:8])}</td>"
            f"<td>{escape(job['created_at'])}</td>"
            "</tr>"
        )

    position_body = "".join(position_rows) or "<tr><td colspan='11'>No positions yet.</td></tr>"
    job_body = "".join(job_rows) or "<tr><td colspan='7'>No jobs yet.</td></tr>"

    return f"""
    <!doctype html>
    <html>
      <head>
        <meta charset='utf-8' />
        <title>Trade Control</title>
        <style>
          body {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background:#0b1020; color:#e5e7eb; margin:0; padding:24px; }}
          h1, h2 {{ margin:0 0 12px 0; }}
          .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:20px; margin-bottom:24px; }}
          .card {{ background:#121933; border:1px solid #26304d; border-radius:14px; padding:16px; }}
          label {{ display:block; margin:8px 0 4px; }}
          input, select {{ width:100%; padding:8px; background:#0f172a; color:#e5e7eb; border:1px solid #334155; border-radius:8px; }}
          button {{ padding:8px 12px; border-radius:8px; border:none; background:#2563eb; color:white; cursor:pointer; }}
          table {{ width:100%; border-collapse: collapse; }}
          th, td {{ border-bottom:1px solid #26304d; padding:8px; text-align:left; vertical-align:top; }}
          .muted {{ color:#94a3b8; }}
          .wide {{ grid-column: 1 / -1; }}
        </style>
      </head>
      <body>
        <h1>Trade Control Dashboard</h1>
        <p class='muted'>Railway is acting as the source of truth for risk gating, active trade count, and stop orchestration.</p>
        <div class='grid'>
          <section class='card'>
            <h2>Runtime Settings</h2>
            <p>Execution: {render_bool_badge(settings['execution_enabled'], 'Enabled', 'Disabled')}</p>
            <p>Auto stop-loss: {render_bool_badge(settings['auto_submit_stop_loss'], 'Enabled', 'Disabled')}</p>
            <p>Active positions: <strong>{active_count}</strong> / {settings['max_open_positions']}</p>
            <p>Open contracts per account: <strong>{active_contract_count}</strong> / <strong>{settings['max_open_contracts_per_account']}</strong></p>
            <p>Mirrored accounts: <strong>{settings['mirrored_account_count']}</strong></p>
            <p>Allowed risk per trade: <strong>{settings['min_risk_dollars']:.0f}</strong> to <strong>{settings['max_risk_dollars']:.0f}</strong> per account</p>
          </section>
          <section class='card'>
            <h2>Controls</h2>
            <form method='post' action='/dashboard/settings'>
              <label for='execution_enabled'>Execution enabled</label>
              <select name='execution_enabled' id='execution_enabled'>
                <option value='true' {"selected" if settings['execution_enabled'] else ""}>true</option>
                <option value='false' {"selected" if not settings['execution_enabled'] else ""}>false</option>
              </select>
              <label for='auto_submit_stop_loss'>Auto-submit stop loss</label>
              <select name='auto_submit_stop_loss' id='auto_submit_stop_loss'>
                <option value='true' {"selected" if settings['auto_submit_stop_loss'] else ""}>true</option>
                <option value='false' {"selected" if not settings['auto_submit_stop_loss'] else ""}>false</option>
              </select>
              <label for='max_open_positions'>Max open positions</label>
              <input type='number' min='1' step='1' name='max_open_positions' id='max_open_positions' value='{settings['max_open_positions']}' />
              <label for='max_open_contracts_per_account'>Max open contracts per account</label>
              <input type='number' min='1' step='1' name='max_open_contracts_per_account' id='max_open_contracts_per_account' value='{settings['max_open_contracts_per_account']}' />
              <label for='min_risk_dollars'>Min risk dollars</label>
              <input type='number' min='0' step='1' name='min_risk_dollars' id='min_risk_dollars' value='{settings['min_risk_dollars']:.0f}' />
              <label for='max_risk_dollars'>Max risk dollars</label>
              <input type='number' min='0' step='1' name='max_risk_dollars' id='max_risk_dollars' value='{settings['max_risk_dollars']:.0f}' />
              <label for='mirrored_account_count'>Mirrored account count</label>
              <input type='number' min='1' step='1' name='mirrored_account_count' id='mirrored_account_count' value='{settings['mirrored_account_count']}' />
              <div style='margin-top:12px'><button type='submit'>Save Settings</button></div>
            </form>
          </section>
          <section class='card wide'>
            <h2>Positions</h2>
            <table>
              <thead>
                <tr>
                  <th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Risk</th><th>Stop</th><th>Status</th><th>Created</th><th>Opened</th><th>Closed</th><th>Action</th>
                </tr>
              </thead>
              <tbody>{position_body}</tbody>
            </table>
          </section>
          <section class='card wide'>
            <h2>Recent Jobs</h2>
            <table>
              <thead>
                <tr>
                  <th>ID</th><th>Type</th><th>Symbol</th><th>Side</th><th>Status</th><th>Position</th><th>Created</th>
                </tr>
              </thead>
              <tbody>{job_body}</tbody>
            </table>
          </section>
        </div>
      </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def root_dashboard() -> str:
    return render_dashboard_html()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> str:
    return render_dashboard_html()


@app.post("/dashboard/settings")
def update_dashboard_settings(
    execution_enabled: str = Form(...),
    auto_submit_stop_loss: str = Form(...),
    max_open_positions: int = Form(...),
    max_open_contracts_per_account: int = Form(...),
    min_risk_dollars: float = Form(...),
    max_risk_dollars: float = Form(...),
    mirrored_account_count: int = Form(...),
) -> RedirectResponse:
    runtime_settings["execution_enabled"] = execution_enabled.lower() == "true"
    runtime_settings["auto_submit_stop_loss"] = auto_submit_stop_loss.lower() == "true"
    runtime_settings["max_open_positions"] = max(1, int(max_open_positions))
    runtime_settings["max_open_contracts_per_account"] = max(1, int(max_open_contracts_per_account))
    runtime_settings["min_risk_dollars"] = max(0.0, float(min_risk_dollars))
    runtime_settings["max_risk_dollars"] = max(float(max_risk_dollars), runtime_settings["min_risk_dollars"])
    runtime_settings["mirrored_account_count"] = max(1, int(mirrored_account_count))
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/positions/{position_id}/close")
def close_position_from_dashboard(position_id: str) -> RedirectResponse:
    close_position(position_id, reason="dashboard_manual_close")
    return RedirectResponse(url="/dashboard", status_code=303)


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
