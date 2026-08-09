#!/usr/bin/env python3
"""ChatGPT login control daemon.

Serves an HTTP control API on 127.0.0.1:18100 for the agent to drive a real
Chromium (under Xvfb) through the chatgpt.com login, and to harvest the web
session once logged in.

API (POST /ctrl, JSON {"op": ...}):
  goto {"url"}          navigate, print URL
  shot {}               screenshot -> returns png path
  eval {"js"}           run JS, return result
  click {"sel"}         click first matching CSS selector
  fill {"sel","text"}   fill input, then press Enter
  press {"key"}         keyboard press
  save {"path"?}        snapshot localStorage + cookies to state.json
  current               current URL
  title                 document.title
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from browser import BASE, ChatGPTBrowser

SHOT_DIR = BASE
os.makedirs(SHOT_DIR, exist_ok=True)
browser = ChatGPTBrowser(stealth=True)
browser.start()
NETLOG = []
def _capture(resp):
    if "auth.openai.com" in resp.url and resp.status >= 400:
        body = ""
        try:
            body = resp.text()[:300]
        except Exception:
            pass
        NETLOG.append({"url": resp.url[:200], "status": resp.status, "body": body})
        if len(NETLOG) > 60:
            NETLOG.pop(0)
try:
    browser.page.on("response", _capture)
except Exception:
    pass
# Warm the context like a normal browsing session before hitting protected sites.
try:
    browser.page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
except Exception:
    pass
import time as _t
_t.sleep(2)
browser.page.goto("https://chatgpt.com/auth/login?screen_hint=password", wait_until="domcontentloaded", timeout=90000)
lock = threading.Lock()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            req = {}
        op = req.get("op")
        try:
            with lock:
                if op == "goto":
                    browser.page.goto(req["url"], timeout=90000)
                    time.sleep(2.0)
                    self._send({"ok": True, "url": browser.page.url})
                elif op == "shot":
                    path = os.path.join(SHOT_DIR, "latest.png")
                    browser.page.screenshot(path=path)
                    self._send({"ok": True, "path": path})
                elif op == "eval":
                    self._send({"ok": True, "result": browser.page.evaluate(req["js"])})
                elif op == "click":
                    sel = req["sel"]
                    browser.page.click(sel, timeout=15000)
                    time.sleep(1.5)
                    self._send({"ok": True, "url": browser.page.url})
                elif op == "fill":
                    browser.page.fill(req["sel"], req["text"], timeout=15000)
                    if req.get("enter"):
                        browser.page.press(req["sel"], "Enter")
                        time.sleep(2.0)
                    self._send({"ok": True})
                elif op == "press":
                    browser.page.keyboard.press(req["key"])
                    time.sleep(1.5)
                    self._send({"ok": True})
                elif op == "ck":
                    dom = req.get("d", "https://auth.openai.com/")
                    ck = browser.context.cookies(dom)
                    self._send({"ok": True, "cookies": [{"n": c["name"], "d": c["domain"],
                                                         "v": (c["value"][:24] + "...") if len(c["value"]) > 24 else c["value"]}
                                                        for c in ck]})
                elif op == "waitclear":
                    deadline = time.time() + int(req.get("timeout", 300))
                    while time.time() < deadline:
                        title = browser.page.title()
                        txt = ""
                        try:
                            txt = browser.page.evaluate("document.body ? document.body.innerText.slice(0,120) : ''")
                        except Exception:
                            pass
                        if "Just a moment" not in title and "Performing security" not in txt:
                            self._send({"ok": True, "cleared": True,
                                        "url": browser.page.url, "title": title})
                            return
                        time.sleep(8)
                    self._send({"ok": False, "error": "clearance timeout", "title": title})
                elif op == "waitts":
                    # Wait until the Turnstile widget has produced a token
                    # (window.turnstile.getResponse() non-empty) or the page
                    # navigated away from challenges.
                    deadline = time.time() + int(req.get("timeout", 120))
                    while time.time() < deadline:
                        try:
                            r = browser.page.evaluate("""(() => {
                              if (typeof window.turnstile === 'undefined') return {kind:'no-widget'};
                              let tok = '';
                              try { tok = window.turnstile.getResponse() || ''; } catch(e) {}
                              return {kind:'widget', tokLen: String(tok).length};
                            })()""")
                            if r.get("tokLen", 0) > 10:
                                self._send({"ok": True, "turnstile": r})
                                return
                        except Exception:
                            pass
                        time.sleep(5)
                    self._send({"ok": False, "error": "turnstile token timeout", "state": r})
                elif op == "netlog":
                    self._send({"ok": True, "net": NETLOG[-30:]})
                elif op == "current":
                    self._send({"ok": True, "url": browser.page.url,
                                "title": browser.page.title()})
                elif op == "save":
                    sn = browser.save_state()
                    self._send({"ok": True, "url": sn["url"],
                                "cookies": len(sn["cookies"]),
                                "ls_keys": list(sn["localStorage"].keys())})
                else:
                    self._send({"ok": False, "error": f"unknown op {op}"})
        except Exception as e:
            self._send({"ok": False, "error": f"{type(e).__name__}: {str(e)[:400]}"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18100
    srv = HTTPServer(("127.0.0.1", port), H)
    print(f"LOGIN_DAEMON_READY {srv.server_address}", flush=True)
    srv.serve_forever()