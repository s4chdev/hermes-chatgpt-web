"""ChatGPT web session manager for the adapter.

Holds a persistent headful Chromium (under Xvfb) logged into chatgpt.com,
and gives the adapter a live web-scoped token + browser cookie context so
backend-api/conversation calls look like the real web app.
"""
import json
import os
import time

from playwright.sync_api import sync_playwright

# Runtime state lives under ~/.chatgpt-adapter by default (override with CHATGPT_HOME).
BASE = os.path.expanduser(os.environ.get("CHATGPT_HOME", "~/.chatgpt-adapter"))
PROFILE = os.path.join(BASE, "profile")  # persistent Chromium profile dir
STATE = os.path.join(BASE, "state.json")  # harvested token/cookies snapshot
os.makedirs(PROFILE, exist_ok=True)

# Match the timezone of the machine that owns the ChatGPT session when possible.
# Override with CHATGPT_TZ (IANA name). Default matches the original deployment.
TIMEZONE = os.environ.get("CHATGPT_TZ", "Asia/Kolkata")

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-gpu",
    "--use-angle=swiftshader",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,900",
]

STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
"""


class ChatGPTBrowser:
    def __init__(self, display=":99", stealth=True):
        self.display = display
        self.stealth = stealth
        self.playwright = None
        self.context = None
        self.page = None

    def start(self):
        os.environ["DISPLAY"] = self.display
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=PROFILE,
            headless=False,
            args=LAUNCH_ARGS,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id=TIMEZONE,
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        if self.stealth:
            self.page.add_init_script(STEALTH)
            try:
                from playwright_stealth import Stealth
                Stealth().apply_stealth_sync(self.page)
            except Exception:
                pass
        return self

    def shot(self, path=None):
        path = path or os.path.join(BASE, "latest.png")
        self.page.screenshot(path=path)
        return path

    def current(self):
        return self.page.url

    def eval(self, js):
        return self.page.evaluate(js)

    def localStorage(self):
        return self.eval(
            "JSON.stringify(Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])))"
        )

    def save_state(self):
        """Snapshot web token + cookies usable by the adapter."""
        ls = json.loads(self.localStorage() or "{}")
        cookies = self.context.cookies("https://chatgpt.com")
        snapshot = {
            "saved_at": time.time(),
            "url": self.page.url,
            "localStorage": ls,
            "cookies": cookies,
        }
        with open(STATE, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.chmod(STATE, 0o600)
        return snapshot

    def stop(self):
        try:
            self.context.close()
        except Exception:
            pass
        try:
            self.playwright.stop()
        except Exception:
            pass
