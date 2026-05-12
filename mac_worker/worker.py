import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Error, Page, sync_playwright
from AppKit import NSScreen

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


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TradovateExecutor:
    def __init__(
        self,
        tradovate_url: str,
        user_data_dir: str,
        dry_run: bool,
        username: str | None,
        password: str | None,
        prepare_only: bool,
        enable_send: bool,
        stop_menu_click_x: float | None,
        stop_menu_click_y: float | None,
        verbose: bool,
    ) -> None:
        self.tradovate_url = tradovate_url
        self.user_data_dir = user_data_dir
        self.dry_run = dry_run
        self.username = username
        self.password = password
        self.prepare_only = prepare_only
        self.enable_send = enable_send
        self.stop_menu_click_x = stop_menu_click_x
        self.stop_menu_click_y = stop_menu_click_y
        self.verbose = verbose
        self.playwright = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    def _log(self, level: str, message: str, *, verbose_only: bool = False) -> None:
        if verbose_only and not self.verbose:
            return
        print(f"[{level}] {message}", flush=True)

    def _debug(self, message: str) -> None:
        self._log("debug", message, verbose_only=True)

    def _info(self, message: str) -> None:
        self._log("info", message)

    def _ok(self, message: str) -> None:
        self._log("ok", message)

    def _warn(self, message: str) -> None:
        self._log("warn", message)

    def _error(self, message: str) -> None:
        self._log("error", message)

    @staticmethod
    def _opposite_side(side: str) -> str:
        side_upper = side.upper()
        if side_upper == "LONG":
            return "SHORT"
        if side_upper == "SHORT":
            return "LONG"
        raise RuntimeError(f"Unsupported side for stop handling: {side}")

    @staticmethod
    def _price_decimals(tick_size: float) -> int:
        tick_text = f"{tick_size:.10f}".rstrip("0").rstrip(".")
        if "." not in tick_text:
            return 0
        return len(tick_text.split(".")[1])

    def _format_price(self, price: float, tick_size: float) -> str:
        decimals = self._price_decimals(tick_size)
        return f"{price:.{decimals}f}"

    def _native_stop_order_shortcut(self) -> bool:
        applescript = """
        tell application "Chromium" to activate
        delay 0.15
        tell application "System Events"
            key code 125
            delay 0.08
            key code 125
            delay 0.08
            key code 125
            delay 0.08
            key code 36
        end tell
        """
        try:
            self._debug("Trying native macOS STOP shortcut via osascript")
            completed = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                self._debug(f"osascript STOP shortcut failed: {stderr}")
                return False
            time.sleep(0.6)
            return True
        except Exception as exc:
            self._debug(f"Native STOP shortcut raised exception: {exc}")
            return False

    def _native_cliclick(self, *commands: str) -> bool:
        try:
            completed = subprocess.run(
                ["/opt/homebrew/bin/cliclick", "-w", "80", *commands],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                stderr = (completed.stderr or "").strip()
                self._debug(f"cliclick failed: {stderr}")
                return False
            return True
        except Exception as exc:
            self._debug(f"cliclick raised exception: {exc}")
            return False

    def _activate_chromium(self) -> None:
        applescript = """
        tell application "Chromium" to activate
        """
        try:
            subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            pass

    def _native_click_point(self, x: float, y: float) -> bool:
        self._activate_chromium()
        time.sleep(0.2)
        return self._native_cliclick(
            f"m:{int(x)},{int(y)}",
            f"c:{int(x)},{int(y)}",
        )

    def _screen_height(self) -> int | None:
        try:
            screen = NSScreen.mainScreen()
            if screen is None:
                return None
            frame = screen.frame()
            return int(frame.size.height)
        except Exception:
            return None

    def _dom_click_at(self, x: float, y: float) -> bool:
        if self.page is None:
            return False
        try:
            return bool(
                self.page.evaluate(
                    """
                    ([x, y]) => {
                      const el = document.elementFromPoint(x, y);
                      if (!el) return false;
                      const events = ["pointerdown", "mousedown", "pointerup", "mouseup", "click"];
                      for (const type of events) {
                        const evt = new MouseEvent(type, {
                          bubbles: true,
                          cancelable: true,
                          composed: true,
                          clientX: x,
                          clientY: y,
                          button: 0,
                          buttons: 1,
                          view: window
                        });
                        el.dispatchEvent(evt);
                      }
                      return true;
                    }
                    """,
                    [x, y],
                )
            )
        except Exception:
            return False

    def _dom_select_order_type_menu_item(self, target_label: str, min_y: float) -> bool:
        if self.page is None:
            return False
        try:
            result = self.page.evaluate(
                """
                ({ minY, targetLabel }) => {
                  function isVisible(el) {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
                  }

                  function normalizedText(el) {
                    return ((el.innerText || el.textContent || "").trim().replace(/\\s+/g, " "));
                  }

                  function clickableAncestor(el) {
                    let cur = el;
                    while (cur && cur !== document.body) {
                      const role = cur.getAttribute?.("role") || "";
                      const tag = (cur.tagName || "").toUpperCase();
                      const onclick = typeof cur.onclick === "function";
                      if (
                        tag === "A" ||
                        tag === "BUTTON" ||
                        tag === "LI" ||
                        role === "option" ||
                        role === "menuitem" ||
                        onclick
                      ) {
                        return cur;
                      }
                      cur = cur.parentElement;
                    }
                    return el;
                  }

                  function clickLikeUser(el) {
                    const rect = el.getBoundingClientRect();
                    const x = rect.x + rect.width / 2;
                    const y = rect.y + rect.height / 2;
                    const events = ["pointerover", "mouseover", "pointerenter", "mouseenter", "pointermove", "mousemove", "pointerdown", "mousedown", "pointerup", "mouseup", "click"];
                    for (const type of events) {
                      const evt = new MouseEvent(type, {
                        bubbles: true,
                        cancelable: true,
                        composed: true,
                        clientX: x,
                        clientY: y,
                        button: 0,
                        buttons: 1,
                        view: window
                      });
                      el.dispatchEvent(evt);
                    }
                    if (typeof el.focus === "function") el.focus();
                    if (typeof el.click === "function") el.click();
                    return { x, y };
                  }

                  const all = Array.from(document.querySelectorAll("*"));
                  const candidates = [];
                  for (const el of all) {
                    if (!isVisible(el)) continue;
                    if (normalizedText(el) !== targetLabel) continue;
                    const clickable = clickableAncestor(el);
                    if (!isVisible(clickable)) continue;
                    const rect = clickable.getBoundingClientRect();
                    if (rect.y <= minY + 20) continue;
                    candidates.push({
                      textTag: el.tagName,
                      clickTag: clickable.tagName,
                      role: clickable.getAttribute?.("role") || "",
                      className: clickable.className || "",
                      x: rect.x,
                      y: rect.y,
                      width: rect.width,
                      height: rect.height,
                      bg: window.getComputedStyle(clickable).backgroundColor || ""
                    });
                  }

                  candidates.sort((a, b) => a.y - b.y);
                  if (candidates.length === 0) {
                    return { ok: false, candidates: [] };
                  }

                  const chosenMeta = candidates[0];
                  const chosen = all.find((el) => {
                    if (!isVisible(el)) return false;
                    if (normalizedText(el) !== targetLabel) return false;
                    const clickable = clickableAncestor(el);
                    const rect = clickable.getBoundingClientRect();
                    return (
                      Math.abs(rect.x - chosenMeta.x) < 1 &&
                      Math.abs(rect.y - chosenMeta.y) < 1 &&
                      Math.abs(rect.width - chosenMeta.width) < 1 &&
                      Math.abs(rect.height - chosenMeta.height) < 1
                    );
                  });
                  const clickable = clickableAncestor(chosen);
                  const clicked = clickLikeUser(clickable);
                  return { ok: true, chosen: chosenMeta, clicked, candidates };
                }
                """,
                {"minY": min_y, "targetLabel": target_label},
            )
            if isinstance(result, dict):
                candidates = result.get("candidates", [])
                if candidates:
                    self._debug(f"DOM {target_label} candidates: {candidates}")
                if result.get("ok"):
                    chosen = result.get("chosen", {})
                    clicked = result.get("clicked", {})
                    self._debug(
                        f"DOM {target_label} chose "
                        f"tag={chosen.get('clickTag')} role={chosen.get('role')} "
                        f"class={chosen.get('className')} rect=({chosen.get('x')}, {chosen.get('y')}, {chosen.get('width')}, {chosen.get('height')}) "
                        f"clicked=({clicked.get('x')}, {clicked.get('y')})"
                    )
                    return True
            return False
        except Exception:
            return False

    def _dom_select_stop_menu_item(self, min_y: float) -> bool:
        return self._dom_select_order_type_menu_item("STOP", min_y)

    def _selected_order_type_text(self) -> str:
        if self.page is None:
            return ""
        try:
            modal = self._ticket_modal()
            order_label = modal.get_by_text(re.compile("^ORDER TYPE$", re.I)).first
            if order_label.count() == 0:
                return ""
            label_box = order_label.bounding_box()
            if not label_box:
                return ""
            candidates = []
            for locator in (
                modal.locator("span.form-control"),
                modal.locator(".select-input"),
            ):
                try:
                    for idx in range(locator.count()):
                        candidate = locator.nth(idx)
                        if not candidate.is_visible():
                            continue
                        box = candidate.bounding_box()
                        if not box:
                            continue
                        if abs(box["y"] - label_box["y"]) > 24:
                            continue
                        text = (candidate.inner_text() or "").strip()
                        if text:
                            candidates.append((box["y"], box["x"], text))
                except Exception:
                    continue
            if candidates:
                candidates.sort()
                return candidates[0][2].upper()
        except Exception:
            return ""
        return ""

    def _selected_quantity_text(self) -> str:
        if self.page is None:
            return ""
        try:
            modal = self._ticket_modal()
            qty_label = modal.get_by_text(re.compile("^QTY$", re.I)).first
            if qty_label.count() == 0:
                return ""
            label_box = qty_label.bounding_box()
            if not label_box:
                return ""
            candidates = []
            for locator in (
                modal.locator("span.form-control"),
                modal.locator(".select-input"),
                modal.locator("input[placeholder='Select value']"),
            ):
                try:
                    for idx in range(locator.count()):
                        candidate = locator.nth(idx)
                        if not candidate.is_visible():
                            continue
                        box = candidate.bounding_box()
                        if not box:
                            continue
                        if abs(box["y"] - label_box["y"]) > 24:
                            continue
                        text = ""
                        try:
                            text = (candidate.input_value() or "").strip()
                        except Exception:
                            text = (candidate.inner_text() or "").strip()
                        if text:
                            candidates.append((box["y"], box["x"], text))
                except Exception:
                    continue
            if candidates:
                candidates.sort()
                return candidates[0][2].upper()
        except Exception:
            return ""
        return ""

    def _measure_order_type_menu_item(self, target_label: str, min_y: float) -> dict[str, float] | None:
        if self.page is None:
            return None
        try:
            result = self.page.evaluate(
                """
                ({ minY, targetLabel }) => {
                  function isVisible(el) {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
                  }

                  function normalizedText(el) {
                    return ((el.innerText || el.textContent || "").trim().replace(/\\s+/g, " "));
                  }

                  const all = Array.from(document.querySelectorAll("li, a, [role='option'], [role='menuitem']"));
                  const candidates = all
                    .filter(isVisible)
                    .filter((el) => normalizedText(el) === targetLabel)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      return {
                        x: rect.x,
                        y: rect.y,
                        width: rect.width,
                        height: rect.height,
                        center_x: rect.x + rect.width / 2,
                        center_y: rect.y + rect.height / 2,
                        text: normalizedText(el),
                        tag: (el.tagName || "").toLowerCase(),
                      };
                    })
                    .filter((item) => item.y > minY + 20)
                    .sort((a, b) => a.y - b.y);
                  return candidates.length ? candidates[0] : null;
                }
                """,
                {"minY": min_y, "targetLabel": target_label},
            )
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    def _select_order_type_from_open_dropdown(self, target_label: str) -> bool:
        if self.page is None:
            return False
        try:
            modal = self._ticket_modal()
            open_dropdown = modal.locator(".select-input.open").first
            if open_dropdown.count() == 0:
                return False
            open_dropdown.wait_for(state="visible", timeout=1_500)
            row_candidates = modal.locator(".select-input.open li, .select-input.open a")
            target_regex = re.compile(f"^{re.escape(target_label)}$", re.I)
            count = row_candidates.count()
            for idx in range(count):
                row = row_candidates.nth(idx)
                try:
                    if not row.is_visible():
                        continue
                    text = (row.inner_text() or "").strip().upper()
                    if not target_regex.match(text):
                        continue
                    box = row.bounding_box()
                    if box:
                        self._debug(
                            f"Open dropdown row {target_label} at ({box['x']:.0f}, {box['y']:.0f}, {box['width']:.0f}, {box['height']:.0f})"
                        )
                    row.scroll_into_view_if_needed(timeout=1_000)
                    row.click(timeout=1_000, force=True)
                    return True
                except Exception:
                    continue
            for idx in range(count):
                row = row_candidates.nth(idx)
                try:
                    if not row.is_visible():
                        continue
                    text = (row.inner_text() or "").strip().upper()
                    if not target_regex.match(text):
                        continue
                    row.dispatch_event("click")
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _select_quantity_from_open_dropdown(self, target_label: str) -> bool:
        if self.page is None:
            return False
        try:
            modal = self._ticket_modal()
            open_dropdown = modal.locator(".select-input.open").first
            if open_dropdown.count() == 0:
                return False
            open_dropdown.wait_for(state="visible", timeout=1_500)
            row_candidates = modal.locator(".select-input.open li, .select-input.open a")
            target_regex = re.compile(f"^{re.escape(target_label)}$", re.I)
            count = row_candidates.count()
            for idx in range(count):
                row = row_candidates.nth(idx)
                try:
                    if not row.is_visible():
                        continue
                    text = (row.inner_text() or "").strip().upper()
                    if not target_regex.match(text):
                        continue
                    box = row.bounding_box()
                    if box:
                        self._debug(
                            f"Open quantity dropdown row {target_label} at ({box['x']:.0f}, {box['y']:.0f}, {box['width']:.0f}, {box['height']:.0f})"
                        )
                    row.scroll_into_view_if_needed(timeout=1_000)
                    row.click(timeout=1_000, force=True)
                    return True
                except Exception:
                    continue
            for idx in range(count):
                row = row_candidates.nth(idx)
                try:
                    if not row.is_visible():
                        continue
                    text = (row.inner_text() or "").strip().upper()
                    if not target_regex.match(text):
                        continue
                    row.dispatch_event("click")
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _select_order_type_by_fixed_row(self, order_type: str) -> bool:
        if self.page is None:
            return False
        order_sequence = ["MARKET", "LIMIT", "STOP", "STOP LIMIT", "TRL STOP", "TRL STP LMT"]
        if order_type.upper() not in order_sequence:
            return False
        try:
            modal = self._ticket_modal()
            rows = modal.locator(".select-input.open li")
            target_index = order_sequence.index(order_type.upper())
            if rows.count() <= target_index:
                return False
            row = rows.nth(target_index)
            if not row.is_visible():
                return False
            box = row.bounding_box()
            if box:
                self._debug(
                    f"Fixed-row {order_type.upper()} at index {target_index} rect="
                    f"({box['x']:.0f}, {box['y']:.0f}, {box['width']:.0f}, {box['height']:.0f})"
                )
            try:
                row.scroll_into_view_if_needed(timeout=1_000)
            except Exception:
                pass
            try:
                row.click(timeout=1_000, force=True)
                return True
            except Exception:
                pass
            try:
                anchor = row.locator("a").first
                if anchor.count() > 0:
                    anchor.click(timeout=1_000, force=True)
                    return True
            except Exception:
                pass
            try:
                row.dispatch_event("click")
                return True
            except Exception:
                pass
            try:
                if box:
                    self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

    def start(self) -> None:
        self._info("Starting Playwright browser")
        self.playwright = sync_playwright().start()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=False,
                args=["--hide-crash-restore-bubble"],
            )
        except Error as exc:
            message = str(exc)
            if "ProcessSingleton" in message or "profile is already in use" in message:
                raise RuntimeError(
                    "Playwright profile is already in use. Close the other worker/browser using "
                    f"{self.user_data_dir} and start again."
                ) from exc
            raise
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        self.page.goto(self.tradovate_url, wait_until="domcontentloaded")
        self._ok(f"Opened Tradovate at {self.tradovate_url}")
        self.maybe_login()
        self.maybe_select_simulation()

    def stop(self) -> None:
        if self.context is not None:
            self.context.close()
        if self.playwright is not None:
            self.playwright.stop()

    def ensure_tradovate_ready(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        self._dismiss_post_send_confirmation_modal()
        self._debug("Refreshing Tradovate page state")
        self.page.goto(self.tradovate_url, wait_until="domcontentloaded")
        self.maybe_login()
        self.maybe_select_simulation()
        self.maybe_open_trading_ticket()

    def is_login_page(self) -> bool:
        if self.page is None:
            return False
        try:
            url = self.page.url.lower()
            password_visible = self.page.locator("input[type='password']").count() > 0 and self.page.locator("input[type='password']").first.is_visible()
            username_text_visible = self.page.get_by_text(re.compile("username", re.I)).count() > 0
            welcome_text_visible = self.page.get_by_text(re.compile("welcome back", re.I)).count() > 0
            login_button_visible = self.page.get_by_role("button", name=re.compile("login", re.I)).count() > 0
            welcome_url = "/welcome" in url
            detected = password_visible or (username_text_visible and login_button_visible) or welcome_text_visible or welcome_url
            self._debug(
                f"Login detection | password_visible={password_visible} "
                f"username_text_visible={username_text_visible} "
                f"welcome_text_visible={welcome_text_visible} "
                f"login_button_visible={login_button_visible} "
                f"welcome_url={welcome_url} "
                f"url={self.page.url}"
            )
            return detected
        except Exception:
            return False

    def maybe_login(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        if not self.is_login_page():
            self._debug("Tradovate login page not detected; assuming active session")
            return
        if not self.username or not self.password:
            raise RuntimeError("Tradovate login required but TRADOVATE_USERNAME/TRADOVATE_PASSWORD are not set")

        self._info("Tradovate login required; attempting login")
        if "/welcome" in self.page.url.lower():
            self.page.wait_for_load_state("domcontentloaded")
            time.sleep(1)
        if self.page.get_by_role("button", name=re.compile("accept cookies", re.I)).count() > 0:
            cookie_button = self.page.get_by_role("button", name=re.compile("accept cookies", re.I)).first
            try:
                cookie_button.scroll_into_view_if_needed(timeout=2_000)
                cookie_button.click(timeout=2_000, force=True)
                self._debug("Accepted cookies banner")
            except Exception as exc:
                self._debug(f"Could not click cookie banner cleanly, continuing anyway: {exc}")

        username_locator = self.page.locator("input[type='text'], input[type='email'], input:not([type])").first
        password_locator = self.page.locator("input[type='password']").first

        username_locator.click()
        username_locator.fill(self.username)
        password_locator.click()
        password_locator.fill(self.password)
        self.page.get_by_role("button", name=re.compile("login", re.I)).first.click()
        self.page.wait_for_load_state("domcontentloaded")

        # Allow Tradovate to transition into the trading UI after login.
        for _ in range(15):
            if not self.is_login_page():
                self._ok("Tradovate login successful")
                return
            time.sleep(1)

        raise RuntimeError("Tradovate login did not complete; manual intervention may be required")

    def is_trading_mode_page(self) -> bool:
        if self.page is None:
            return False
        try:
            heading_visible = self.page.get_by_text(re.compile("select a trading mode", re.I)).count() > 0
            sim_button_visible = self.page.get_by_role(
                "button",
                name=re.compile("access simulation|start simulated trading", re.I),
            ).count() > 0
            detected = heading_visible or sim_button_visible or "trading-mode" in self.page.url.lower()
            self._debug(
                f"Trading mode detection | heading_visible={heading_visible} "
                f"sim_button_visible={sim_button_visible} url={self.page.url}"
            )
            return detected
        except Exception:
            return False

    def maybe_select_simulation(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        if not self.is_trading_mode_page():
            return

        self._info("Selecting Tradovate simulation mode")
        self.page.get_by_role(
            "button",
            name=re.compile("access simulation|start simulated trading", re.I),
        ).first.click()
        self.page.wait_for_load_state("domcontentloaded")

        for _ in range(15):
            if not self.is_trading_mode_page():
                self._ok("Tradovate simulation mode selected")
                return
            time.sleep(1)

        raise RuntimeError("Tradovate trading mode page did not clear after clicking Access Simulation")

    def is_trading_ticket_open(self) -> bool:
        if self.page is None:
            return False
        try:
            simple_tab = self.page.get_by_text(re.compile("^simple$", re.I)).count() > 0
            advanced_tab = self.page.get_by_text(re.compile("^advanced$", re.I)).count() > 0
            order_type_text = self.page.get_by_text(re.compile("order type", re.I)).count() > 0
            send_button = self.page.get_by_role("button", name=re.compile("^send$", re.I)).count() > 0
            qty_text = self.page.get_by_text(re.compile("^qty$", re.I)).count() > 0
            reset_button = self.page.get_by_role("button", name=re.compile("^reset$", re.I)).count() > 0
            # Be stricter here: "ORDER TYPE" text alone can linger in partial/stale UI states.
            # Treat the ticket as open only when we have the core ticket shell plus a real action control.
            detected = (
                ((simple_tab and advanced_tab) or (qty_text and order_type_text))
                and (send_button or reset_button)
            )
            self._debug(
                f"Trading ticket detection | simple_tab={simple_tab} "
                f"advanced_tab={advanced_tab} qty_text={qty_text} "
                f"order_type_text={order_type_text} send_button={send_button} "
                f"reset_button={reset_button} url={self.page.url}"
            )
            return detected
        except Exception:
            return False

    def maybe_open_trading_ticket(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        self._dismiss_post_send_confirmation_modal()
        if self.is_trading_ticket_open():
            self._debug("Tradovate trading ticket already open")
            return

        self._info("Opening Tradovate trading ticket")

        if self.is_login_page():
            self._warn("Tradovate returned to welcome while opening ticket; retrying login flow")
            self.maybe_login()
            self.maybe_select_simulation()
            if self.is_trading_ticket_open():
                self._ok("Trading ticket became available after re-login")
                return

        direct_candidates = [
            self.page.get_by_role("button", name=re.compile("^trading$", re.I)),
            self.page.locator("[aria-label='Trading'], [title='Trading'], [data-tooltip-content='Trading']"),
        ]

        for candidate in direct_candidates:
            try:
                if candidate.count() > 0:
                    button = candidate.first
                    button.scroll_into_view_if_needed(timeout=2_000)
                    button.click(timeout=2_000, force=True)
                    time.sleep(0.5)
                    if self.is_trading_ticket_open():
                        self._ok("Opened trading ticket")
                        return
            except Exception as exc:
                self._debug(f"Direct Trading selector failed, trying fallback: {exc}")

        # Fallback: hover/click top-left controls until the Trading tooltip appears.
        buttons = self.page.locator("button, [role='button'], div, span, a")
        button_count = min(buttons.count(), 120)
        for idx in range(button_count):
            candidate = buttons.nth(idx)
            try:
                box = candidate.bounding_box()
                if not box:
                    continue
                if box["x"] > 320 or box["y"] > 160:
                    continue
                if box["width"] <= 4 or box["height"] <= 4:
                    continue
                candidate.hover(timeout=1_000)
                time.sleep(0.2)
                if self.page.get_by_text(re.compile("^trading$", re.I)).count() > 0:
                    candidate.click(timeout=2_000, force=True)
                    time.sleep(0.5)
                    if self.is_trading_ticket_open():
                        self._ok("Opened trading ticket via tooltip fallback")
                        return
            except Exception:
                continue

        # Give Tradovate a moment to fully restore the main shell after login /
        # simulation changes before we start coordinate clicks.
        time.sleep(1.0)

        # Final fallback: Tradovate's top-left Trading control is canvas-like and
        # may not expose a usable DOM selector. Click a wider cluster around the
        # icon area, retrying in passes.
        try:
            self._debug("Falling back to coordinate click for top-left Trading control")
            candidate_points = []
            for y in (20, 28, 36, 44, 52, 60, 68, 76, 84, 92):
                for x in (70, 82, 94, 106, 118, 130, 142, 154, 166, 178):
                    candidate_points.append((x, y))
            for attempt in range(3):
                self._debug(f"Trading control coordinate pass {attempt + 1}")
                for x, y in candidate_points:
                    self._debug(f"Trying Trading control click at ({x}, {y})")
                    self.page.mouse.move(x, y)
                    time.sleep(0.15)
                    self.page.mouse.click(x, y)
                    time.sleep(1.0)
                    if self.is_login_page():
                        self._warn("Trading control click landed on welcome; retrying login flow")
                        self.maybe_login()
                        self.maybe_select_simulation()
                        time.sleep(1.0)
                        continue
                    if self.is_trading_ticket_open():
                        self._ok("Opened trading ticket via coordinate fallback")
                        return
                time.sleep(0.8)
        except Exception as exc:
            self._debug(f"Coordinate fallback failed: {exc}")

        raise RuntimeError("Could not open standardized Tradovate trading ticket from the Trading button")

    def _dismiss_post_send_confirmation_modal(self) -> None:
        if self.page is None:
            return
        try:
            dialog = self.page.get_by_text(re.compile("group strategy confirmed", re.I)).first
            if dialog.count() == 0 or not dialog.is_visible():
                return
            self._info("Dismissing post-send confirmation modal")
            done_button = self.page.get_by_role("button", name=re.compile("^done$", re.I)).first
            if done_button.count() > 0:
                done_button.click(timeout=2_000, force=True)
                time.sleep(0.4)
                return
            close_buttons = self.page.locator("[role='dialog'] button, [role='dialog'] [role='button']")
            for idx in range(close_buttons.count()):
                try:
                    button = close_buttons.nth(idx)
                    text = (button.inner_text() or "").strip().lower()
                    aria = (button.get_attribute("aria-label") or "").strip().lower()
                    if text in {"x", "close"} or aria in {"close", "dismiss"}:
                        button.click(timeout=1_000, force=True)
                        time.sleep(0.4)
                        return
                except Exception:
                    continue
        except Exception:
            pass

    def _find_visible_text_input(self):
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        inputs = self._ticket_modal().locator("input")
        for idx in range(inputs.count()):
            candidate = inputs.nth(idx)
            try:
                if not candidate.is_visible():
                    continue
                if (candidate.get_attribute("type") or "").lower() == "password":
                    continue
                return candidate
            except Exception:
                continue
        raise RuntimeError("Could not find visible text input in Tradovate ticket")

    def _ticket_modal(self):
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        modal_candidates = self.page.locator("[data-testid='order-ticket-modal'], [role='dialog']")
        count = modal_candidates.count()
        if count > 0:
            return modal_candidates.nth(count - 1)
        send_button = self.page.get_by_role("button", name=re.compile("^send$", re.I)).first
        if send_button.count() > 0:
            return send_button.locator("xpath=ancestor::*[@role='dialog' or @data-testid='order-ticket-modal'][1]")
        raise RuntimeError("Could not find Tradovate order ticket modal")

    def _click_trade_side(self, side: str) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        modal = self._ticket_modal()
        button_name = "Buy" if side.upper() == "LONG" else "Sell"
        candidates = [
            modal.get_by_role("button", name=re.compile(f"^{button_name}$", re.I)),
            modal.get_by_text(re.compile(f"^{button_name}$", re.I)),
        ]
        for candidate in candidates:
            try:
                if candidate.count() > 0:
                    candidate.first.click(timeout=2_000, force=True)
                    self._ok(f"Selected trade side: {button_name}")
                    return
            except Exception:
                continue
        raise RuntimeError(f"Could not select {button_name} button in Tradovate ticket")

    def _set_quantity(self, quantity: int) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        modal = self._ticket_modal()
        target = str(quantity)

        def quantity_selected() -> bool:
            selected_text = self._selected_quantity_text()
            if selected_text:
                return selected_text == target
            try:
                values = modal.locator("input").evaluate_all(
                    """els => els.map(e => (e.value || e.getAttribute('value') || '').trim())"""
                )
                for value in values:
                    if str(value).strip() == target:
                        return True
            except Exception:
                pass
            try:
                qty_texts = modal.locator("div, span, input").evaluate_all(
                    """els => els.map(e => ((e.innerText || e.textContent || e.value || '').trim()))"""
                )
                for value in qty_texts:
                    if str(value).strip() == target:
                        return True
            except Exception:
                pass
            return False

        def try_typed_quantity_commit(qty_input_like, *, click_first: bool = True) -> bool:
            try:
                if click_first:
                    qty_input_like.click(timeout=2_000, force=True)
                    time.sleep(0.15)
            except Exception:
                return False

            for attempt in range(1, 4):
                try:
                    try:
                        qty_input_like.press("Meta+A")
                        time.sleep(0.05)
                        qty_input_like.press("Backspace")
                    except Exception:
                        pass
                    try:
                        qty_input_like.fill("")
                    except Exception:
                        pass
                    time.sleep(0.05)
                    try:
                        qty_input_like.type(target, delay=35)
                    except Exception:
                        self.page.keyboard.type(target, delay=35)
                    time.sleep(0.20)

                    current_value = ""
                    try:
                        current_value = (qty_input_like.input_value() or "").strip()
                    except Exception:
                        pass

                    if current_value == target:
                        try:
                            self.page.keyboard.press("Enter")
                            time.sleep(0.20)
                        except Exception:
                            pass
                        if quantity_selected():
                            self._ok(f"Selected quantity: {quantity}")
                            return True
                        try:
                            self.page.keyboard.press("Tab")
                            time.sleep(0.20)
                        except Exception:
                            pass
                        if quantity_selected():
                            self._ok(f"Selected quantity: {quantity}")
                            return True
                    elif quantity_selected():
                        self._ok(f"Selected quantity: {quantity}")
                        return True

                    self._debug(
                        f"Typed quantity commit attempt {attempt} did not stick | target={target} | seen={current_value or 'blank'}"
                    )
                except Exception:
                    continue
            return False

        # First try a true input path if Tradovate exposes one.
        qty_input = modal.locator("input[placeholder='Select value']").first
        if qty_input.count() > 0:
            if try_typed_quantity_commit(qty_input):
                return

        # Fallback: treat quantity as a custom combo box anchored to the QTY label.
        qty_label = modal.get_by_text(re.compile("^QTY$", re.I)).first
        if qty_label.count() == 0:
            raise RuntimeError("Could not find QTY label in Tradovate ticket")

        label_box = qty_label.bounding_box()
        if not label_box:
            raise RuntimeError("Could not measure QTY label in Tradovate ticket")

        # Click into the quantity field area to the right of the QTY label.
        field_x = label_box["x"] + 150
        field_y = label_box["y"] + 6

        self._debug(f"Trying quantity field click at ({field_x:.0f}, {field_y:.0f})")
        if try_typed_quantity_commit(type("MouseField", (), {
            "click": lambda _self, **kwargs: self.page.mouse.click(field_x, field_y),
            "press": lambda _self, key: self.page.keyboard.press(key),
            "fill": lambda _self, value: None,
            "type": lambda _self, value, delay=35: self.page.keyboard.type(value, delay=delay),
            "input_value": lambda _self: "",
        })(), click_first=True):
            return

        raise RuntimeError(f"Could not commit typed quantity {quantity} in Tradovate field")

    def _set_order_type(self, order_type: str) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        modal = self._ticket_modal()
        order_label = modal.get_by_text(re.compile("^ORDER TYPE$", re.I)).first
        if order_label.count() == 0:
            raise RuntimeError("Could not find ORDER TYPE label in Tradovate ticket")

        label_box = order_label.bounding_box()
        if not label_box:
            raise RuntimeError("Could not measure ORDER TYPE label in Tradovate ticket")

        field_x = label_box["x"] + 155
        field_y = label_box["y"] + 8
        arrow_x = label_box["x"] + 315
        arrow_y = label_box["y"] + 8

        self._debug(f"Trying order type field click at ({field_x:.0f}, {field_y:.0f})")
        self.page.mouse.click(field_x, field_y)
        time.sleep(0.4)

        order_sequence = ["MARKET", "LIMIT", "STOP", "STOP LIMIT", "TRL STOP", "TRL STP LMT"]

        def order_type_selected() -> bool:
            selected_text = self._selected_order_type_text()
            if selected_text:
                return selected_text == order_type.upper()
            if order_type.upper() == "STOP":
                try:
                    if modal.get_by_text(re.compile("^PRICE$", re.I)).count() > 0:
                        return True
                except Exception:
                    pass
            try:
                field_text = modal.locator("input").evaluate_all(
                    """els => els.map(e => (e.value || e.getAttribute('value') || '').trim())"""
                )
                for value in field_text:
                    if str(value).strip().upper() == order_type.upper():
                        return True
            except Exception:
                pass
            try:
                return modal.get_by_text(re.compile(f"^{re.escape(order_type)}$", re.I)).count() > 0 and modal.get_by_text(re.compile("^ORDER TYPE$", re.I)).count() > 0
            except Exception:
                return False

        try:
            self._debug(f"Trying fixed-row dropdown selection for {order_type.upper()}")
            self.page.mouse.click(arrow_x, arrow_y)
            time.sleep(0.30)
            if self._select_order_type_by_fixed_row(order_type.upper()):
                time.sleep(0.5)
                if order_type_selected():
                    self._ok(f"Selected order type: {order_type}")
                    return
        except Exception:
            pass

        try:
            self._debug(f"Trying strict open-dropdown row selection for {order_type.upper()}")
            self.page.mouse.click(field_x, field_y)
            time.sleep(0.30)
            if self._select_order_type_from_open_dropdown(order_type.upper()):
                time.sleep(0.5)
                if order_type_selected():
                    self._ok(f"Selected order type: {order_type}")
                    return
        except Exception:
            pass

        current_selected = self._selected_order_type_text()
        if current_selected in order_sequence and order_type.upper() in order_sequence and current_selected != order_type.upper():
            try:
                self._debug(
                    f"Trying relative order type shortcut from {current_selected} to {order_type.upper()}"
                )
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.25)
                current_index = order_sequence.index(current_selected)
                target_index = order_sequence.index(order_type.upper())
                diff = target_index - current_index
                key = "ArrowDown" if diff > 0 else "ArrowUp"
                for _ in range(abs(diff)):
                    self.page.keyboard.press(key)
                    time.sleep(0.07)
                self.page.keyboard.press("Enter")
                time.sleep(0.6)
                if order_type_selected():
                    self._ok(f"Selected order type: {order_type}")
                    return
            except Exception:
                pass

        if order_type.upper() == "MARKET":
            try:
                self._debug("Trying measured MARKET menu-row click")
                self.page.mouse.click(field_x, field_y)
                time.sleep(0.35)
                market_item = self._measure_order_type_menu_item("MARKET", field_y)
                if market_item:
                    self._debug(
                        f"Measured MARKET row at ({market_item['center_x']:.0f}, {market_item['center_y']:.0f}) "
                        f"size=({market_item['width']:.0f}x{market_item['height']:.0f})"
                    )
                    self.page.mouse.click(market_item["center_x"], market_item["center_y"])
                    time.sleep(0.6)
                    if order_type_selected():
                        self._ok("Selected order type: MARKET")
                        return
            except Exception:
                pass

            try:
                self._debug("Trying literal MARKET text / clickable-parent selector")
                self.page.mouse.click(field_x, field_y)
                time.sleep(0.35)
                if self._dom_select_order_type_menu_item("MARKET", field_y):
                    time.sleep(0.6)
                    if order_type_selected():
                        self._ok("Selected order type: MARKET")
                        return
            except Exception:
                pass

            try:
                self._debug("Trying explicit MARKET shortcut: open menu, ArrowUp x8, Enter")
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.25)
                for _ in range(8):
                    self.page.keyboard.press("ArrowUp")
                    time.sleep(0.06)
                self.page.keyboard.press("Enter")
                time.sleep(0.6)
                if order_type_selected():
                    self._ok("Selected order type: MARKET")
                    return
            except Exception:
                pass

        # For Tradovate STOP orders, keep the path brutally simple:
        # 1. click the MARKET order-type field body
        # 2. native-click the exact STOP screen coordinate
        # 3. verify PRICE row appears
        if order_type.upper() == "STOP":
            try:
                self._debug("Trying literal STOP text / clickable-parent selector")
                self.page.mouse.click(field_x, field_y)
                time.sleep(0.35)
                if self._dom_select_stop_menu_item(field_y):
                    time.sleep(0.6)
                    if order_type_selected():
                        self._ok("Selected order type: STOP")
                        return
            except Exception:
                pass

        if order_type.upper() == "STOP" and self.stop_menu_click_x is not None and self.stop_menu_click_y is not None:
            try:
                native_points = [(self.stop_menu_click_x, self.stop_menu_click_y)]
                screen_h = self._screen_height()
                if screen_h is not None:
                    converted_y = screen_h - self.stop_menu_click_y
                    if abs(converted_y - self.stop_menu_click_y) > 1:
                        native_points.append((self.stop_menu_click_x, converted_y))

                self._debug(
                    f"Trying direct STOP native click path with field ({field_x:.0f}, {field_y:.0f}) and stop candidates "
                    f"{[(int(x), int(y)) for x, y in native_points]}"
                )
                self.page.mouse.click(field_x, field_y)
                time.sleep(0.45)
                for stop_x, stop_y in native_points:
                    self._debug(f"Trying native STOP click candidate ({int(stop_x)}, {int(stop_y)})")
                    if not self._native_click_point(stop_x, stop_y):
                        continue
                    time.sleep(0.7)
                    if order_type_selected():
                        self._ok("Selected order type: STOP")
                        return
                    self.page.mouse.click(field_x, field_y)
                    time.sleep(0.30)
            except Exception:
                pass

        option_candidates = [
            self.page.get_by_role("option", name=re.compile(f"^{re.escape(order_type)}$", re.I)),
            self.page.get_by_text(re.compile(f"^{re.escape(order_type)}$", re.I)),
        ]
        for candidate in option_candidates:
            try:
                if candidate.count() > 0:
                    candidate.last.click(timeout=1_000, force=True)
                    time.sleep(0.3)
                    if order_type_selected():
                        self._ok(f"Selected order type: {order_type}")
                        return
            except Exception:
                continue

        self._debug(f"Direct click did not stick for order type {order_type}; trying keyboard fallback")
        try:
            click_x = arrow_x if order_type.upper() == "STOP" else field_x
            click_y = arrow_y if order_type.upper() == "STOP" else field_y
            self._debug(f"Trying order type keyboard anchor click at ({click_x:.0f}, {click_y:.0f})")
            self.page.mouse.click(click_x, click_y)
            time.sleep(0.2)
            # Tradovate consistently opens from MARKET in this ticket flow.
            # For STOP specifically, the reliable path is 3x ArrowDown then Enter.
            if order_type.upper() == "STOP":
                for _ in range(3):
                    self.page.keyboard.press("ArrowDown")
                    time.sleep(0.10)
            else:
                for _ in range(8):
                    self.page.keyboard.press("ArrowUp")
                    time.sleep(0.05)
                if order_type.upper() not in order_sequence:
                    raise RuntimeError(f"Unsupported order type sequence for {order_type}")
                target_index = order_sequence.index(order_type.upper())
                for _ in range(target_index):
                    self.page.keyboard.press("ArrowDown")
                    time.sleep(0.08)
            self.page.keyboard.press("Enter")
            time.sleep(0.5)
            if order_type_selected():
                self._ok(f"Selected order type: {order_type}")
                return
        except Exception:
            pass

        # Last fallback for STOP specifically: click the field, send 3x ArrowDown,
        # then Enter without relying on DOM option selection.
        if order_type.upper() == "STOP":
            try:
                self._debug("Trying explicit STOP shortcut: ArrowDown x3 then Enter")
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.2)
                self.page.keyboard.press("ArrowDown")
                time.sleep(0.10)
                self.page.keyboard.press("ArrowDown")
                time.sleep(0.10)
                self.page.keyboard.press("ArrowDown")
                time.sleep(0.10)
                self.page.keyboard.press("Enter")
                time.sleep(0.5)
                if order_type_selected():
                    self._ok("Selected order type: STOP")
                    return
            except Exception:
                pass

        if order_type.upper() == "STOP":
            try:
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.25)
                if self._native_stop_order_shortcut() and order_type_selected():
                    self._ok("Selected order type: STOP")
                    return
            except Exception:
                pass

        # Final fallback: Tradovate's order type menu has a fixed vertical
        # layout. Click the STOP row directly relative to the order-type field.
        if order_type.upper() == "STOP":
            try:
                if self.stop_menu_click_x is not None and self.stop_menu_click_y is not None:
                    self._debug(
                        f"Trying calibrated STOP menu click at ({self.stop_menu_click_x:.0f}, {self.stop_menu_click_y:.0f})",
                    )
                    self.page.mouse.click(arrow_x, arrow_y)
                    time.sleep(0.35)
                    self.page.mouse.move(self.stop_menu_click_x, self.stop_menu_click_y)
                    time.sleep(0.20)
                    self.page.mouse.down()
                    time.sleep(0.08)
                    self.page.mouse.up()
                    time.sleep(0.6)
                    if order_type_selected():
                        self._ok("Selected order type: STOP")
                        return
            except Exception:
                pass

        if order_type.upper() == "STOP":
            try:
                if self.stop_menu_click_x is not None and self.stop_menu_click_y is not None:
                    self._debug(
                        f"Trying native cliclick STOP menu click at screen ({self.stop_menu_click_x:.0f}, {self.stop_menu_click_y:.0f})",
                    )
                    self.page.mouse.click(arrow_x, arrow_y)
                    time.sleep(0.25)
                    self._activate_chromium()
                    time.sleep(0.2)
                    if self._native_cliclick(f"c:{int(self.stop_menu_click_x)},{int(self.stop_menu_click_y)}"):
                        time.sleep(0.6)
                        if order_type_selected():
                            self._ok("Selected order type: STOP")
                            return
            except Exception:
                pass

        if order_type.upper() == "STOP":
            try:
                self._debug("Trying visible STOP label click fallback")
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.25)
                stop_labels = self.page.get_by_text(re.compile("^STOP$", re.I))
                for idx in range(stop_labels.count()):
                    try:
                        stop_label = stop_labels.nth(idx)
                        box = stop_label.bounding_box()
                        if not box:
                            continue
                        # Ignore the closed field value and focus on the open menu row below it.
                        if box["y"] <= field_y + 20:
                            continue
                        center_x = box["x"] + box["width"] / 2
                        center_y = box["y"] + box["height"] / 2
                        self._debug(f"Trying visible STOP label click at ({center_x:.0f}, {center_y:.0f})")
                        self.page.mouse.click(center_x, center_y)
                        time.sleep(0.4)
                        if order_type_selected():
                            self._ok("Selected order type: STOP")
                            return
                        self.page.mouse.click(arrow_x, arrow_y)
                        time.sleep(0.2)
                    except Exception:
                        continue
            except Exception:
                pass

        # Final fallback: Tradovate's order type menu has a fixed vertical
        # layout. Click the STOP row directly relative to the order-type field.
        if order_type.upper() == "STOP":
            try:
                self._debug("Trying direct STOP menu-row click fallback")
                self.page.mouse.click(arrow_x, arrow_y)
                time.sleep(0.25)
                stop_row_points = [
                    (field_x, field_y + 126),
                    (field_x - 24, field_y + 126),
                    (field_x + 24, field_y + 126),
                    (field_x, field_y + 112),
                    (field_x, field_y + 140),
                ]
                for menu_x, menu_y in stop_row_points:
                    self._debug(f"Trying STOP menu-row click at ({menu_x:.0f}, {menu_y:.0f})")
                    self.page.mouse.click(menu_x, menu_y)
                    time.sleep(0.4)
                    if order_type_selected():
                        self._ok("Selected order type: STOP")
                        return
                    # Re-open the menu before trying another row point.
                    self.page.mouse.click(arrow_x, arrow_y)
                    time.sleep(0.2)
            except Exception:
                pass

        raise RuntimeError(f"Could not select order type {order_type} in Tradovate ticket")

    def _set_price(self, price_text: str) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        modal = self._ticket_modal()
        price_label = modal.get_by_text(re.compile("^PRICE$", re.I)).first
        if price_label.count() == 0:
            raise RuntimeError("Could not find PRICE label in Tradovate ticket")

        label_box = price_label.bounding_box()
        if not label_box:
            raise RuntimeError("Could not measure PRICE label in Tradovate ticket")

        field_x = label_box["x"] + 150
        field_y = label_box["y"] + 8

        self._debug(f"Trying price field click at ({field_x:.0f}, {field_y:.0f})")
        self.page.mouse.click(field_x, field_y)
        time.sleep(0.2)
        self.page.keyboard.press("Meta+A")
        self.page.keyboard.press("Backspace")
        self.page.keyboard.type(price_text, delay=50)
        time.sleep(0.3)

        self._ok(f"Typed stop price: {price_text}")

    def _set_symbol(self, symbol: str) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        search_input = self._find_visible_text_input()
        target = symbol.strip().upper()
        last_seen = ""
        for attempt in range(1, 4):
            search_input.click(timeout=2_000)
            time.sleep(0.10)
            try:
                search_input.press("Meta+A")
                time.sleep(0.05)
                search_input.press("Backspace")
            except Exception:
                pass
            search_input.fill("")
            time.sleep(0.05)
            search_input.type(symbol, delay=35)
            time.sleep(0.20)

            selected = False
            try:
                option = self.page.locator("li, [role='option'], a").filter(
                    has_text=re.compile(f"^{re.escape(symbol)}$", re.I)
                ).first
                if option.count() > 0:
                    option.click(timeout=700, force=True)
                    selected = True
                    time.sleep(0.30)
            except Exception:
                pass

            if not selected:
                try:
                    search_input.press("Enter")
                    time.sleep(0.30)
                except Exception:
                    pass

            try:
                last_seen = (search_input.input_value() or "").strip().upper()
            except Exception:
                last_seen = ""

            if last_seen == target:
                self._ok(f"Selected symbol: {symbol}")
                return

            self._debug(
                f"Symbol selection attempt {attempt} did not stick | target={target} | seen={last_seen or 'blank'}"
            )

        raise RuntimeError(f"Could not confirm Tradovate symbol selection for {symbol}; last seen value was {last_seen or 'blank'}")

    def prepare_trade_ticket(self, symbol: str, side: str, quantity: int) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        self.ensure_tradovate_ready()
        self._set_symbol(symbol)
        self._click_trade_side(side)
        self._set_quantity(quantity)
        self._set_order_type("MARKET")
        self._ok("Prepared entry ticket without sending")

    def prepare_stop_ticket(
        self,
        symbol: str,
        entry_side: str,
        quantity: int,
        stop_price: float,
        tick_size: float,
    ) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        self.ensure_tradovate_ready()
        stop_side = self._opposite_side(entry_side)
        self._set_symbol(symbol)
        self._click_trade_side(stop_side)
        self._set_quantity(quantity)
        self._set_order_type("STOP")
        self._set_price(self._format_price(stop_price, tick_size))
        self._ok("Prepared STOP loss ticket without sending")

    def submit_trade_ticket(self) -> None:
        if self.page is None:
            raise RuntimeError("Browser page is not initialized")
        if not self.enable_send:
            raise RuntimeError("Live send blocked because ENABLE_SEND is not true")
        modal = self._ticket_modal()
        send_button = modal.get_by_role("button", name=re.compile("^send$", re.I)).first
        if send_button.count() == 0:
            raise RuntimeError("Could not find Send button in Tradovate ticket")
        send_button.click(timeout=2_000, force=True)
        self._ok("Pressed Send on Tradovate ticket")
        time.sleep(1.0)

    def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        self.ensure_tradovate_ready()
        request_payload = job["request"]
        plan = job["plan"]
        ticket_type = str(request_payload.get("ticket_type", "entry")).lower()

        result = {
            "mode": "dry_run" if self.dry_run else "live",
            "ticket_type": ticket_type,
            "symbol": request_payload["symbol"],
            "side": request_payload["side"],
            "quantity": plan["quantity"],
            "entry_price": plan["entry_price"],
            "effective_stop_price": plan["effective_stop_price"],
            "risk_per_contract": plan["risk_per_contract"],
        }

        if self.prepare_only:
            if ticket_type == "stop_loss":
                self.prepare_stop_ticket(
                    symbol=request_payload["symbol"],
                    entry_side=request_payload["side"],
                    quantity=plan["quantity"],
                    stop_price=plan["effective_stop_price"],
                    tick_size=plan["tick_size"],
                )
            else:
                self.prepare_trade_ticket(
                    symbol=request_payload["symbol"],
                    side=request_payload["side"],
                    quantity=plan["quantity"],
                )
            result["mode"] = "prepare_only"
            return result

        if self.dry_run:
            self._info(f"DRY RUN plan: {result}")
            return result

        if ticket_type == "stop_loss":
            self.prepare_stop_ticket(
                symbol=request_payload["symbol"],
                entry_side=request_payload["side"],
                quantity=plan["quantity"],
                stop_price=plan["effective_stop_price"],
                tick_size=plan["tick_size"],
            )
        else:
            self.prepare_trade_ticket(
                symbol=request_payload["symbol"],
                side=request_payload["side"],
                quantity=plan["quantity"],
            )
        self.submit_trade_ticket()
        result["mode"] = "live_send"
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
    prepare_only = os.getenv("PREPARE_ONLY", "true").lower() == "true"
    enable_send = os.getenv("ENABLE_SEND", "false").lower() == "true"
    verbose = os.getenv("WORKER_VERBOSE", "false").lower() == "true"
    max_job_age_seconds = max(int(os.getenv("MAX_JOB_AGE_SECONDS", "300")), 0)
    tradovate_url = os.getenv("TRADOVATE_URL", "https://trader.tradovate.com/")
    user_data_dir = os.environ["PLAYWRIGHT_USER_DATA_DIR"]
    tradovate_username = os.getenv("TRADOVATE_USERNAME")
    tradovate_password = os.getenv("TRADOVATE_PASSWORD")
    stop_menu_click_x = float(os.getenv("STOP_MENU_CLICK_X", "757"))
    stop_menu_click_y = float(os.getenv("STOP_MENU_CLICK_Y", "515"))

    if enable_send and (dry_run or prepare_only):
        print("[warn] ENABLE_SEND=true is ignored unless DRY_RUN=false and PREPARE_ONLY=false", flush=True)

    mode = "dry-run" if dry_run else "prepare-only" if prepare_only else "live-send-armed" if enable_send else "live-send-blocked"
    print(
        f"[info] Starting worker {worker_id} | mode={mode} "
        f"| poll={poll_seconds}s | verbose={verbose} | max_job_age={max_job_age_seconds}s",
        flush=True,
    )
    railway = RailwayClient(base_url, worker_id)
    executor = TradovateExecutor(
        tradovate_url,
        user_data_dir,
        dry_run,
        tradovate_username,
        tradovate_password,
        prepare_only,
        enable_send,
        stop_menu_click_x,
        stop_menu_click_y,
        verbose,
    )
    executor.start()

    try:
        while True:
            executor.maybe_login()
            executor.maybe_select_simulation()
            executor.maybe_open_trading_ticket()
            try:
                job = railway.claim_next_job()
            except httpx.HTTPError as exc:
                executor._warn(f"Railway poll failed: {exc}. Retrying soon...")
                time.sleep(poll_seconds)
                continue
            if job is None:
                executor._debug("No pending jobs. Polling again soon...")
                time.sleep(poll_seconds)
                continue

            created_at = _parse_iso_datetime(job.get("created_at"))
            if created_at is not None and max_job_age_seconds > 0:
                age_seconds = int((datetime.now(timezone.utc) - created_at).total_seconds())
                if age_seconds > max_job_age_seconds:
                    reason = f"Stale job blocked: age {age_seconds}s exceeds MAX_JOB_AGE_SECONDS={max_job_age_seconds}s"
                    try:
                        railway.fail_job(
                            job["job_id"],
                            reason,
                            extra={"blocked_as_stale": True, "job_age_seconds": age_seconds},
                        )
                    except httpx.HTTPError as exc:
                        executor._warn(f"Could not mark stale job {job['job_id'][:8]} as failed: {exc}")
                    executor._warn(f"Rejected stale job {job['job_id'][:8]} | age={age_seconds}s")
                    time.sleep(poll_seconds)
                    continue

            executor._info(
                f"Claimed job {job['job_id'][:8]} | type={job['request'].get('ticket_type', 'entry')} "
                f"| symbol={job['request']['symbol']} | side={job['request']['side']}"
            )
            try:
                result = executor.execute_job(job)
                railway.complete_job(job["job_id"], result)
                executor._ok(f"Completed job {job['job_id'][:8]}")
            except Exception as exc:
                railway.fail_job(job["job_id"], str(exc))
                executor._error(f"Failed job {job['job_id'][:8]}: {exc}")
            time.sleep(poll_seconds)
    finally:
        executor.stop()


if __name__ == "__main__":
    main()
