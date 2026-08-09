#!/usr/bin/env python3
"""SPA gateway: owns ONE persistent Chromium session on chatgpt.com and answers
/chat requests by driving the real composer. Single-threaded (Playwright sync API).

Local HTTP API:
  GET  /status        -> {"ok": true, "logged_in": bool, "title": "..."}
  POST /chat          -> {"prompt": "...", "model": "..."} -> {"text": "..."}
"""
import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from browser import ChatGPTBrowser

from http.server import BaseHTTPRequestHandler, HTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))


def _default_cookie_file():
    """Resolve cookie map path. Prefer CHATGPT_COOKIE_FILE, then ~/.chatgpt-adapter,
    then the legacy /tmp path used by the original deployment."""
    env = os.environ.get("CHATGPT_COOKIE_FILE")
    if env:
        return env
    home = os.path.expanduser(os.environ.get("CHATGPT_HOME", "~/.chatgpt-adapter"))
    candidate = os.path.join(home, "cookies_parsed.json")
    if os.path.exists(candidate):
        return candidate
    legacy = "/tmp/cookies_parsed.json"
    if os.path.exists(legacy):
        return legacy
    return candidate


COOKIE_FILE = _default_cookie_file()
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18110

SECURE = {"__Secure-next-auth.session-token.0", "__Secure-next-auth.session-token.1",
          "__Secure-oai-is", "__Secure-next-auth.callback-url", "__Host-next-auth.csrf-token",
          "cf_clearance", "__cf_bm", "_cfuvid"}
HOST_ONLY = {"__Host-next-auth.csrf-token", "__Secure-next-auth.callback-url"}

_state = {"ready": False, "error": None, "title": "", "lock": threading.Lock()}


def boot():
    pairs = json.load(open(COOKIE_FILE))
    b = ChatGPTBrowser().start()
    ctx = b.context
    page = b.page
    _state["browser"] = b
    _state["page"] = page
    for name, value in pairs:
        secure = name in SECURE
        dms = ["chatgpt.com"] if name in HOST_ONLY else [".chatgpt.com", "chatgpt.com", ".openai.com", ".auth.openai.com"]
        for dm in dms:
            try:
                ctx.add_cookies([{"name": name, "value": value, "domain": dm, "path": "/",
                                  "secure": secure, "sameSite": "Lax"}])
            except Exception:
                pass
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)
    # quiet CF wait
    t0 = time.time()
    while time.time() - t0 < 420:
        time.sleep(15)
        try:
            cur = (page.title() or "").strip()
        except Exception:
            cur = ""
        if "just a moment" not in cur.lower() and "security" not in cur.lower() and cur:
            break
    _state["title"] = (page.title() or "")
    time.sleep(8)
    # close welcome modal if present
    try:
        for sel in ['[data-testid="close-button"]', "button:has-text('Close')"]:
            try:
                btn = page.locator(sel).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3000)
                    break
            except Exception:
                pass
    except Exception:
        pass
    _state["ok"] = True


def new_chat(page):
    try:
        btn = page.locator('[data-testid="create-new-chat-button"]').first
        if btn.count():
            btn.click(timeout=6000)
            time.sleep(0.8)
    except Exception:
        pass


def ask_stream(page, prompt, model=None, reset=False):
    """Drive the composer and YIELD incremental assistant text plus a done event.

    reset=True: click the New chat button first (fresh conversation).
    Otherwise the CURRENT conversation thread is reused, so ChatGPT can cache
    the ~30k context prefix and only pay for the new delta.

    Yields dicts: {"delta": str, "text": str} for new tokens, then
    {"done": True, "text": final}. {"error": ...} on failure.
    Single-flight via the global lock (blocked in HTTP layer).
    """
    model = model or "auto"
    with _state["lock"]:
        if reset:
            new_chat(page)
        # count pre-existing assistant turns so we only emit NEW message text
        try:
            turns0 = page.evaluate(
                "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length")
        except Exception:
            turns0 = 0
        try:
            ta = page.locator("#prompt-textarea").first
            ta.click(timeout=30000)
        except Exception as e:
            yield {"error": f"composer not found: {str(e)[:200]}"}
            return
        # insert via execCommand: instant regardless of prompt size
        try:
            inserted = page.evaluate("""(txt) => {
                const el = document.querySelector('#prompt-textarea');
                if (!el) return false;
                el.focus();
                const ok = document.execCommand('insertText', false, txt);
                return ok === true || el.innerText.trim().length > 0;
            }""", prompt)
            if not inserted:
                raise RuntimeError("insertText returned falsy")
        except Exception:
            try:
                page.keyboard.type(prompt, delay=15)
            except Exception:
                yield {"error": "prompt insert failed"}
                return
        time.sleep(0.3)
        try:
            page.keyboard.press("Enter")
        except Exception:
            yield {"error": "send failed"}
            return
        # fast poll loop: emit text growth as deltas, detect completion
        t0 = time.time()
        last = ""
        idle = 0
        while time.time() - t0 < 480:
            time.sleep(0.25)
            try:
                msgs = page.evaluate(
                    """JSON.stringify([...document.querySelectorAll('[data-message-author-role="assistant"]')]
                        .map(el => el.innerText))""")
                arr = json.loads(msgs or "[]")
                if len(arr) > turns0:
                    cur = arr[turns0]
                    if cur != last:
                        if cur.startswith(last) and last:
                            delta = cur[len(last):]
                        else:
                            delta = cur
                        last = cur
                        idle = 0
                        if delta:
                            yield {"delta": delta, "text": cur}
                    elif cur:
                        idle += 1
            except Exception:
                pass
            # completion: new turn arrived, generator button gone, composer re-enabled, text stable
            try:
                done = page.evaluate(
                    """(() => {
                        const spb = document.querySelector('[data-testid="stop-button"]');
                        const gen = spb && spb.offsetParent !== null;
                        const sbtn = document.querySelector('[data-testid="send-button"], button[type="submit"]');
                        const dis = sbtn ? sbtn.disabled : null;
                        const turns = document.querySelectorAll('[data-message-author-role="assistant"]').length;
                        return JSON.stringify({gen, dis, turns});
                    })()""")
                s = json.loads(done or "{}")
                if s.get("turns", 0) > turns0 and not s.get("gen", True) and s.get("dis", False) is False and idle >= 3:
                    break
            except Exception:
                pass
            if idle > 45:
                break
        yield {"done": True, "text": last}


def ask(page, prompt, model=None, reset=False):
    """Non-stream compat: run the stream, return {text} or {error}."""
    out = ""
    error = None
    for ev in ask_stream(page, prompt, model, reset=reset):
        if "error" in ev:
            error = ev["error"]
        elif ev.get("done"):
            out = ev.get("text", out)
    if error:
        return {"error": error}
    return {"text": out}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_event(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path.startswith("/status"):
            try:
                turns = _state["page"].evaluate(
                    "document.querySelectorAll('[data-message-author-role=\"assistant\"]').length")
            except Exception:
                turns = None
            self._send(200, {"ok": _state["ok"], "title": _state["title"], "error": _state["error"], "turns": turns})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/chat"):
            self._send(404, {"error": "not found"})
            return
        ln = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        if not _state["ok"]:
            self._send(503, {"error": _state["error"] or "gateway not booted"})
            return
        is_stream = self.path.startswith("/chat/stream")
        reset = bool(body.get("reset", False))
        try:
            gen = ask_stream(_state["page"], body.get("prompt", ""), body.get("model", "auto"), reset=reset)
            if not is_stream:
                # drain the generator, return final result as JSON
                res = {"text": ""}
                error = None
                for ev in gen:
                    if "error" in ev:
                        error = ev["error"]
                    elif ev.get("done"):
                        res["text"] = ev.get("text", res["text"])
                if error:
                    self._send(500, {"error": error})
                else:
                    self._send(200, res)
                return
            # SSE streaming response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            error = None
            for ev in gen:
                if "error" in ev:
                    error = ev["error"]
                    ev = {"error": error}
                self._sse_event(ev)
                if error:
                    break
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send(500, {"error": str(e)[:500]})
            except Exception:
                pass


def main():
    print(f"COOKIE_FILE {COOKIE_FILE}", flush=True)
    boot()
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"GATEWAY_UP {PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()