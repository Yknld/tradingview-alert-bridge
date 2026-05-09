import os
import re
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page, sync_playwright

load_dotenv()


class RailwayClient:
    def __init__(self, base_url: str, worker_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.http = httpx.Client(timeout=30.0)

    def claim_next_job(self) -> dict[str, Any] | None:
        response = self.http.get(
            f"{self.base_url}/execution/jobs/next",
            params={"worker_id": self.worker_id},
        )
        response.raise_for_status()
        return response.json().get("job")

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        response = self.http.post(
            f"{self.base_url}/execution/jobs/{job_id}/complete",
            json=result,
        )
        response.raise_for_status()

    def fail_job(self, job_id: str, reason: str, extra: dict[str, Any] | None = None) -> None:
        payload = {"reason": reason}
        if extra:
            payload.update(extra)
        response = self.http.post(
            f"{self.base_url}/execution/jobs/{job_id}/fail",
            json=payload,
        )
        response.raise_for_status()


class TradovateExecutor:
    def __init__(self, tradovate_url: str, user_data_dir: str, dry_run: bool, username: str | None, password: str | None) -> None:
        self.tradovate_url = tradovate_url
        self.user_data_dir = user_data_dir
        self.dry_run = dry_run
        self.username = username
        self.password = password
        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def start(self) -> None:
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            self.user_data_dir,
            headless=False,
        )
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        self.page.goto(self.tradovate_url, wait_until="domcontentloaded")

    def stop(self) -> None:
        if self.context is not None:
            self.context.close()
        if self.playwright is not None:
            self.playwright.stop()

    def ensure_tradovate_ready(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        self.page.goto(self.tradovate_url, wait_until="domcontentloaded")
        self.maybe_login()

    def is_login_page(self) -> bool:
        if self.page is None:
            return False
        try:
            return self.page.locator("input[type='password']").first.is_visible(timeout=2000)
        except Exception:
            return False

    def maybe_login(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        if not self.is_login_page():
            return
        if not self.username or not self.password:
            raise RuntimeError("Tradovate login required but TRADOVATE_USERNAME/TRADOVATE_PASSWORD are not set")

        username_input = self.page.get_by_label(re.compile("username|email", re.I))
        password_input = self.page.locator("input[type='password']").first
        if username_input.count() == 0:
            username_input = self.page.locator("input[type='text'], input:not([type])").first

        username_input.first.fill(self.username)
        password_input.fill(self.password)
        self.page.get_by_role("button", name=re.compile("login", re.I)).first.click()
        self.page.wait_for_load_state("domcontentloaded")

        # Allow Tradovate to transition into the trading UI after login.
        for _ in range(15):
            if not self.is_login_page():
                print("Tradovate login successful", flush=True)
                return
            time.sleep(1)

        raise RuntimeError("Tradovate login did not complete; manual intervention may be required")

    def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        self.ensure_tradovate_ready()
        request_payload = job["request"]
        plan = job["plan"]

        result = {
            "mode": "dry_run" if self.dry_run else "live",
            "symbol": request_payload["symbol"],
            "side": request_payload["side"],
            "quantity": plan["quantity"],
            "entry_price": plan["entry_price"],
            "effective_stop_price": plan["effective_stop_price"],
            "risk_per_contract": plan["risk_per_contract"],
        }

        if self.dry_run:
            print("DRY RUN execution plan:", result, flush=True)
            return result

        # Step-by-step live execution will be added and tested manually:
        # 1. detect logged-out state and log in if needed
        # 2. open Trading ticket
        # 3. search symbol
        # 4. choose buy/sell
        # 5. set quantity
        # 6. send market order
        # 7. place protective stop immediately after fill
        raise NotImplementedError("Live Tradovate order entry is not enabled yet")


def main() -> None:
    base_url = os.environ["RAILWAY_BASE_URL"]
    worker_id = os.getenv("WORKER_ID", "mac-worker")
    poll_seconds = float(os.getenv("POLL_SECONDS", "2"))
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    tradovate_url = os.getenv("TRADOVATE_URL", "https://trader.tradovate.com/")
    user_data_dir = os.environ["PLAYWRIGHT_USER_DATA_DIR"]
    tradovate_username = os.getenv("TRADOVATE_USERNAME")
    tradovate_password = os.getenv("TRADOVATE_PASSWORD")

    railway = RailwayClient(base_url, worker_id)
    executor = TradovateExecutor(tradovate_url, user_data_dir, dry_run, tradovate_username, tradovate_password)
    executor.start()

    try:
        while True:
            job = railway.claim_next_job()
            if job is None:
                time.sleep(poll_seconds)
                continue

            print(f"Claimed job {job['job_id']} for {job['request']['symbol']} {job['request']['side']}", flush=True)
            try:
                result = executor.execute_job(job)
                railway.complete_job(job["job_id"], result)
                print(f"Completed job {job['job_id']}", flush=True)
            except Exception as exc:
                railway.fail_job(job["job_id"], str(exc))
                print(f"Failed job {job['job_id']}: {exc}", flush=True)
            time.sleep(poll_seconds)
    finally:
        executor.stop()


if __name__ == "__main__":
    main()
