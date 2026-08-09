# Agent build spec: ChatGPT Web Adapter

This is a **build-from-scratch spec** for AI coding agents. Read it fully,
then implement. Goal: an OpenAI-compatible API (`/v1/chat/completions`) that
answers from a real chatgpt.com web session via Playwright browser
automation, consuming the account's general web chat quota (not API quota).

Reference: the original was built at `/home/ubuntu/chatgpt-adapter` and is
running (gateway:18110, adapter:18111). You are reproducing it, not
modifying it. Target environment: Ubuntu 22.04+, Python 3.10-3.12, arm64
or x64. If any selector or timing differs in the live SPA, adapt using the
SAME behavioral contract (below) and log what you changed.

---

## 1. System contract (must match exactly)

- Two processes, both loopback-bound:
  - **gateway** on `127.0.0.1:18110` (owns the browser, single-threaded)
  - **adapter** on `127.0.0.1:18111` (OpenAI-compatible API)
- One persistent Chromium session (playwright sync API) under Xvfb on
  display `:99`, launched with `launch_persistent_context` so the browser
  profile survives restarts (Cloudflare clearance persists).
- The SPA session is authenticated with cookies injected from a pasted
  browser session (see §4). The browser must NOT rely on any manual
  login for normal operation.
- Single-flight: exactly ONE chat request at a time. The gateway is a
  single-threaded `http.server.HTTPServer` (playwright sync API is
  thread-bound; never thread it). The adapter streams from the gateway.
- No API keys anywhere. Auth = the injected browser session.

## 2. Deliverables (files to create)

| File | Role |
|------|------|
| `browser.py` | `ChatGPTBrowser` class: launcher + stealth + state snapshot |
| `gateway.py` | SPA-driving HTTP server (boot, ask, stream, status) |
| `adapter.py` | FastAPI OpenAI-compatible front (delta protocol, SSE translation) |
| `session_inject.py` | Cookie-paste -> profile injection -> session harvest |
| `login.py` | Optional manual-login control daemon (port 18100) |
| `run_gateway.sh` / `run_adapter.sh` | Service launchers (Xvfb check + exec venv python) |
| `requirements.txt` | playwright, playwright-stealth, fastapi, uvicorn, httpx |

Package layout: `browser.py` lives next to the adapter/gateway; runtime
state goes in `~/.chatgpt-adapter/` (profile dir, state.json, chmod 600).

---

## 3. Browser layer (`browser.py`)

**Launch** (key parameters):
- `chromium.launch_persistent_context(user_data_dir=~/.chatgpt-adapter/profile,
  headless=False, viewport 1280x800, locale en-US, timezone Asia/Kolkata)`
- Launch args: `--no-sandbox --disable-gpu --use-angle=swiftshader
  --disable-dev-shm-usage --disable-blink-features=AutomationControlled
  --window-size=1280,900`
- Stealth init script on the page:
  - `navigator.webdriver` -> undefined
  - `navigator.languages` -> `['en-US','en']`
  - plugins array spoofed, `window.chrome = {runtime:{}}`
- Also apply `playwright_stealth.Stealth().apply_stealth_sync(page)` if
  importable (wrap in try/except).

**State snapshot** (`save_state`): write → `state.json` (chmod 600) with
`saved_at`, `url`, `localStorage` (json), `cookies` (all chatgpt.com
cookies). Screen dump helper → `latest.png`. `localStorage()` helper reads
all localStorage as JSON (used to find the Bearer token key).

**Hard rules:**
- Never use headless=True with this SPA; the automation detection trips.
- Keep the SAME profile dir across restarts, or Cloudflare re-challenges.
- Pre-warm with `goto("https://www.google.com/")` before any protected
  navigation (please treat as a session-warming heuristic).

---

## 4. Cookie/session contract

The machine cannot complete the OpenAI password login (device/proof
checks). Session setup = paste the user's logged-in browser cookies:

1. User copies the full `cookie:` header value of any chatgpt.com request
   from DevTools > Network (after reload, logged in).
2. They save a JSON: `{"token": "", "cookies": "name=value; n2=v2; ..."}`
3. `session_inject.py`:
   - parse the `;`-separated pairs
   - for each: `add_cookies` with domain candidates
     `[".chatgpt.com", "chatgpt.com", ".openai.com", ".auth.openai.com"]`,
     `secure=True` for names starting `__Secure` or `_dd_s`; secure=False
     otherwise; `sameSite Lax`, path `/`.
   - navigate to chatgpt.com, wait out any "Just a moment" interstitial
     (up to 4 min, sleeping BEFORE reading page.title, title-based check
     `just a moment not in title`), settle 8s
   - harvest storage state (`ctx.storage_state()`) -> `storage_state.json`;
     probe localStorage keys (`accessToken`, `access_token`, ...) for a
     bearer; taste logged_in = url contains chatgpt.com and body text does
     not start with "log in"
4. The cookie map the gateway boot reads lives at `/tmp/cookies_parsed.json`:
   an array of `{name, value}` pairs.
   - Secure-required cookie names: `__Secure-next-auth.session-token.0/1
     __Secure-oai-is __Secure-next-auth.callback-url
     __Host-next-auth.csrf-token cf_clearance __cf_bm _cfuvid`
   - Host-only cookie names (domain = `chatgpt.com` only):
     `__Host-next-auth.csrf-token __Secure-next-auth.callback-url`
   - Everything else: set on `.chatgpt.com`, `chatgpt.com`, `.openai.com`,
     `.auth.openai.com` (attempt all four; ignore exceptions).

**Gateway boot sequence** (after cookie injection):
1. `page.goto("https://chatgpt.com/", wait_until domcontentloaded, 120s)`
2. CF wait loop: up to 420s, poll every 15s; break when
   `just a moment`/`security` not in `page.title()` and title non-empty.
   (Sleep BEFORE reading title.)
3. Settle 8s; close any welcome modal: `[data-testid="close-button"]`
   first, fallback `button:has-text('Close')`, click only if visible.
4. Mark `ready`. Serve requests.

---

## 5. Gateway (`gateway.py`) - behavioral contract

HTTP API (all JSON):
- `GET /status` → `{"ok": bool, "title": str, "error": null|str, "turns": int}`
  where turns = current assistant-message count in the live thread
  (`document.querySelectorAll('[data-message-author-role="assistant"]').length`).
- `POST /chat` → body `{"prompt": str, "model": str?, "reset": bool?}` →
  200 `{"text": str}` or 500 `{"error": str}`. Non-streaming (drain generator).
- `POST /chat/stream` → same body → SSE stream of
  `{"delta": str, "text": str}` events and a final `{"done": true, "text": str}`.
  Errors as `{"error": str}` events.
- Anything else → 404 `{"error": "not found"}`. Bad JSON → 400.
  Not booted → 503 with the boot error.

### one ask (the whole point): `ask_stream(page, prompt, model?, reset?)`

1. Global `threading.Lock` acquired for the entire turn (single-flight).
2. `reset=True`: click `[data-testid="create-new-chat-button"]` (try, 6s,
   swallow errors, sleep 0.8s).
3. Count `turns0` = assistant messages currently in the DOM.
4. Focus composer: click `#prompt-textarea` (30s timeout). If missing →
   yield `{"error": ...}`.
5. INSERT the prompt with:
   `document.execCommand('insertText', false, txt)` on the focused
   `#prompt-textarea` (instant for large prompts). Verify it actually
   inserted (execCommand returned truthy OR element innerText non-empty).
   Fallback: `page.keyboard.type(prompt, delay=15)`.
6. sleep 0.3s, `page.keyboard.press("Enter")`.
7. Poll every 0.25s for up to 480s:
   - Read ALL assistant messages as JSON string → parse
     (`document.querySelectorAll('[data-message-author-role="assistant"]')`
     mapped to innerText).
   - If count > turns0: current = arr[turns0]; if != last → emit
     `{"delta": growth, "text": current}`; reset idle on growth.
   - Completion when ALL: turns > turns0 AND stop button
     (`[data-testid="stop-button"]`) not visible (offsetParent null) AND
     send button not disabled AND idle >= 3 polls. Stop-button/send-button
     checked via one JS evaluate returning JSON.
   - Idle cap 45 (give up, emit what we have).
8. Yield `{"done": true, "text": last}`.

Native context: one `ChatGPTBrowser` per process, booted once in `main()`,
then a forever `serve_forever`. Boot errors captured in a `_state["error"]`
so /status and request paths can report 503.

## 6. Adapter (`adapter.py`) — OpenAI-compatible front

FastAPI app on 127.0.0.1:18111.

- `GET /v1/models` → `{"object":"list","data":[{"id":"gpt-5.6-luna",...}]}`
  (mirror the current chatgpt.com model label; it drifts. Old labels like
  gpt-5.5/gpt-5 don't exist anymore; SPA decides the actual model.)
- `GET /health` → `{"ok": true, "gateway": <status_json>}`.
- `POST /v1/chat/completions`:
  - body: OpenAI messages, optional `stream`
  - **Prompt build**: system messages become `[system] {content}` lines;
    others become `[{role}] {content}`; join with `\n` into one string.
  - **Delta protocol (MUST)**: keep global `_prev_prompt`.
    - `_prev_prompt is not None` AND `prompt.startswith(_prev_prompt)` AND
      longer → send ONLY `prompt[len(_prev_prompt):]` to the gateway with
      `"reset": false` (continue same thread, server-side context cache).
    - else → send FULL prompt with `"reset": true` (new conversation).
    - Store `_prev_prompt = prompt` after every request.
  - Stream backend: `httpx.stream("POST", gateway/chat/stream, json=body,
    timeout=600)`; parse `data:` SSE lines into dicts; first event doubles
    as the health check of the gateway (raise → 502).
  - stream=true → OpenAI `chat.completion.chunk` SSE (`delta`,
    `finish_reason:"stop"` on done, final `data: [DONE]`).
  - stream=false → buffer deltas into one `chat.completion` object,
    `usage: null` (SPA doesn't report usage).
  - Gateway errors → OpenAI-style `{"error":{"message":..., "type":
    "backend_error"}}` with 502.

### 7. Services / deployment

- `run_gateway.sh`: check Xvfb :99 (`xdpyinfo -display :99`); if down,
  start `Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &`; then
  `exec .venv/bin/python gateway.py 18110`.
- `run_adapter.sh`: `exec .venv/bin/python -m uvicorn adapter:app
  --host 127.0.0.1 --port 18111`.
- PM2 note (Ubuntu with PM2 6.0.14): a bun-fork bug breaks launching venv
  console scripts directly; ALWAYS go through the `.sh` wrappers with
  `exec`. `pm2 save` after starting; enable pm2-ubuntu.service.
- Auto-start Xvfb at boot if not present.

---

## 8. Acceptance tests (run these, in order)

1. `curl -s http://127.0.0.1:18110/status` → ok true, title "ChatGPT"
2. `curl -s http://127.0.0.1:18111/v1/models` → list with gpt-5.6-luna
3. `curl -s http://127.0.0.1:18111/health` → ok + gateway ok
4. Completion: `curl -s 127.0.0.1:18111/v1/chat/completions -H
   Content-Type:application/json -d '{"model":"gpt-5.6-luna",
   "messages":[{"role":"user","content":"Reply exactly: PONG"}]}'`
   → 200, content "pong" (approx, case-insensitive), `finish_reason
   : "stop"` within 60s.
5. Thread-reuse: after test 4, `/status` turns == 1; then send a second
   tiny prompt "and now X from ABOVE" (a plain extension of the first in
   the SAME conversation); turns should become 2, confirming the same
   thread continued (no reset). On a brand-new request that diverges
   (full new conversation), turns should grow again or a new thread.
6. Streaming: same call with `"stream": true`; expect chunk SSE events
   and `data: [DONE]`.
7. Restart test: kill both PM2 processes, `pm2 restart chatgpt-gateway
   chatgpt-adapter`, status recovers, reuse still works (same profile).

## 9. Constraints / anti-patterns

- NEVER send raw Bearer POSTs to `backend-api/conversation` - immutable IP
  faces 403 "unusual activity" even with a valid token; only the SPA flow
  passes device/proof checks.
- NEVER thread the gateway. One `HTTPServer` with one handler, one lock.
- NEVER read `page.title()` before sleeping in the CF loop (racy).
- Don't guess model names; the SPA defaults to whatever current label
  chatgpt.com uses, `/v1/models` mirrors it.
- Two clients → each request diverges → each gets its own chat; that's
  accepted (constructor).
- Sessions expire silently; flow is re-inject cookie and restart (the
  gateway reads /tmp/cookies_parsed.json at boot only).

## 10. Precedent (build-time notes)

- Original measured: turn1 ~20.2s, reused-thread turn ~15.9s; the delta
  gets you meaningful wins with ~30k context.
- SPA auto-new-chat near context limit: `turns0` logic keeps capturing the
  newest assistant turn; optionally force a pool point by sending
  `"reset": true`.
- All service ports (18100 login, 18110 gateway, 18111 adapter) are
  loopback-bound only; keep them that way.