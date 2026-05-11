import json
import os
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TRADOVATE_URL = os.getenv("TRADOVATE_URL", "https://trader.tradovate.com/")
USER_DATA_DIR = os.environ["PLAYWRIGHT_USER_DATA_DIR"]


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            args=["--hide-crash-restore-bubble"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(TRADOVATE_URL, wait_until="domcontentloaded")
        print("STOP probe active. Open the order type dropdown manually and leave it open.", flush=True)
        last_payload = None
        try:
            while True:
                payload = page.evaluate(
                    """
                    () => {
                      const all = Array.from(document.querySelectorAll('*'));
                      const matches = [];
                      for (const el of all) {
                        const txt = (el.innerText || el.textContent || '').trim();
                        if (txt !== 'STOP') continue;
                        const rect = el.getBoundingClientRect();
                        if (!rect || rect.width <= 0 || rect.height <= 0) continue;
                        const style = window.getComputedStyle(el);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        matches.push({
                          tag: el.tagName,
                          cls: el.className,
                          text: txt,
                          x: rect.x,
                          y: rect.y,
                          w: rect.width,
                          h: rect.height,
                          center_x: rect.x + rect.width / 2,
                          center_y: rect.y + rect.height / 2
                        });
                      }
                      matches.sort((a, b) => a.y - b.y);
                      return matches;
                    }
                    """
                )
                if payload != last_payload:
                    print(json.dumps(payload, indent=2), flush=True)
                    last_payload = payload
                time.sleep(0.5)
        finally:
            try:
                ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
