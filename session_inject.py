#!/usr/bin/env python3
"""Inject a pasted browser session into the VM Chromium and harvest storage state.

Usage: DISPLAY=:99 ./.venv/bin/python session_inject.py <cookies_json_file>
Reads the JSON {token, cookies} pasted from the user's browser, loads cookies into
the profile context, navigates to chatgpt.com, lets the session settle, then dumps:
- storage_state.json (cookies + auth headers for the adapter)
- session_info.json (token, logged-in status, page URL)
"""
import json
import sys
import time
from pathlib import Path

from browser import ChatGPTBrowser

PROFILE = Path.home() / ".chatgpt-adapter" / "profile"
OUT_STATE = Path.home() / "chatgpt-adapter" / "storage_state.json"
OUT_INFO = Path.home() / "chatgpt-adapter" / "session_info.json"


def parse_cookie_line(raw: str) -> dict:
    """'name=value; n2=v2; ...' -> {name: value}"""
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    src = Path(sys.argv[1]).read_text().strip()
    payload = json.loads(src)  # {"token": ..., "cookies": "k=v; ..."}
    tok = payload.get("token")
    cookies = parse_cookie_line(payload.get("cookies", ""))

    # Split cookies by likely domain. chatgpt.com primary; openai.com secondary.
    # Domain heuristics: oai-* and __Secure-* → .chatgpt.com; auth/account ids → place both.
    # Simplest robust approach: set every cookie on .chatgpt.com, then re-navigate to
    # chatgpt.com so the server re-issues cookies for exactly the right domains.
    b = ChatGPTBrowser()
    b.start()
    page = b.page
    ctx = b.context

    # Clear existing cookies, add ours as a wide-domain set (Playwright needs explicit
    # domains, so map likely auth cookies to both .chatgpt.com and .openai.com).
    domains_candidates = [".chatgpt.com", "chatgpt.com", ".openai.com", ".auth.openai.com"]
    added = 0
    for name, value in cookies.items():
        secure = "__Secure" in name or name in ("_dd_s",)
        for dom in domains_candidates:
            try:
                ctx.add_cookies([{
                    "name": name, "value": value, "domain": dom, "path": "/",
                    "secure": secure, "sameSite": "Lax",
                }])
                added += 1
            except Exception:
                pass
    print(f"added {added} cookie assignments")

    # Warm up + go to chatgpt.com
    page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(3)
    page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=90000)
    time.sleep(10)

    # Wait for any CF interstitial to clear (up to 4 min)
    cleared = False
    for _ in range(24):
        try:
            title = page.title()
        except Exception:
            title = ""
        if "just a moment" in title.lower() or "security verification" in title.lower():
            time.sleep(10)
            continue
        cleared = True
        break
    print(f"interstitial cleared: {cleared}, title={page.title()!r}")

    # Settle + harvest
    time.sleep(8)
    info = {
        "url": page.url,
        "title": page.title(),
        "token": None,
        "localStorage_keys": {},
        "logged_in": False,
    }
    try:
        info["localStorage_keys"] = page.evaluate(
            "JSON.stringify(Object.keys(localStorage))"
        )
    except Exception as e:
        info["localStorage_keys"] = f"err:{e}"
    # Look for a bearer token in localStorage (key: 'accessToken' on chatgpt.com)
    for key in ("accessToken", "access_token", "token", "auth_token"):
        try:
            v = page.evaluate(f"localStorage.getItem({key!r})")
            if v:
                info["token"] = v if len(v) < 2048 else v[:2048]
                break
        except Exception:
            pass
    try:
        body = page.evaluate("document.body ? document.body.innerText.slice(0,400) : 'n/a'")
        info["body_head"] = body
        info["logged_in"] = ("chatgpt.com" in info["url"] and "log in" not in body.lower()[:400])
    except Exception as e:
        info["body_head"] = f"err:{e}"

    state = ctx.storage_state()
    OUT_STATE.write_text(json.dumps(state, indent=2))
    OUT_INFO.write_text(json.dumps(info, indent=2))
    print(f"storage state -> {OUT_STATE}")
    print(f"info -> {OUT_INFO}")
    print(json.dumps({k: (v if k != "body_head" else (v[:120] + "…" if isinstance(v, str) else v)) for k, v in info.items()}, indent=2))

    # Keep the browser alive so the adapter can attach later? No: adapter owns its own
    # browser. We close ours after harvest.
    b.stop()


if __name__ == "__main__":
    main()