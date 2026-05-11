import os
import time
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

TRADOVATE_URL = os.getenv('TRADOVATE_URL', 'https://trader.tradovate.com/')
USER_DATA_DIR = os.environ['PLAYWRIGHT_USER_DATA_DIR']

JS = r'''
(() => {
  if (window.__codex_click_probe_installed) return;
  window.__codex_click_probe_installed = true;

  function describe(el) {
    if (!el) return null;
    const txt = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120);
    const rect = el.getBoundingClientRect();
    return {
      tag: el.tagName,
      text: txt,
      role: el.getAttribute('role'),
      cls: el.className,
      x: rect.x,
      y: rect.y,
      w: rect.width,
      h: rect.height,
    };
  }

  document.addEventListener('click', (e) => {
    const target = e.target;
    const payload = {
      kind: 'CLICK_PROBE',
      clientX: e.clientX,
      clientY: e.clientY,
      pageX: e.pageX,
      pageY: e.pageY,
      target: describe(target),
      parent: describe(target && target.parentElement),
    };
    if (window.codexReportClick) {
      window.codexReportClick(payload);
    }
  }, true);

  if (window.codexReportClick) {
    window.codexReportClick({kind: 'READY'});
  }
})();
'''

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(USER_DATA_DIR, headless=False, args=['--hide-crash-restore-bubble'])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.expose_function("codexReportClick", lambda payload: print(payload, flush=True))
    page.goto(TRADOVATE_URL, wait_until='domcontentloaded')
    page.evaluate(JS)
    print('Click calibration active. Open the order type dropdown and click the STOP row once. Press Ctrl+C when done.', flush=True)
    try:
        while True:
            time.sleep(1)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
