# Agent contract: ChatGPT Web Adapter

**Audience:** AI coding agents working in this repo.  
**The implementation already exists.** Do not recreate the files below from
scratch unless a path is missing or the user explicitly asks for a rewrite.

**Use this doc to:**
1. Understand the behavioral contract the code must keep.
2. Verify, debug, or adapt when the chatgpt.com SPA drifts.
3. Port or extend without inventing a different architecture.

Read the matching source file alongside each section. Prefer surgical edits
that preserve the contracts below.

## Goal

An OpenAI-compatible HTTP API (`/v1/chat/completions`) that answers from a
**real chatgpt.com web session** via Playwright + Xvfb. It consumes the
account's **general web chat quota**, not the OpenAI API quota. No API keys.

```
Client  →  adapter.py :18111  →  gateway.py :18110  →  Chromium (Xvfb :99)  →  chatgpt.com
         (OpenAI-shaped)         (SPA driver,         (persistent profile)
                                  single-flight)
```

**Target:** Ubuntu 22.04+, Python 3.10–3.12, arm64 or x64.  
If live SPA selectors/timings drift, keep the **behavioral contract** below and
log what you changed.

---

## 0. Non-negotiables

1. **SPA only.** Never `POST` to `backend-api/conversation` with a Bearer token
   — that gets `403 unusual activity`. Only drive the real web UI.
2. **One browser, one lock.** Gateway = sync Playwright + single-threaded
   `http.server.HTTPServer`. Never thread the browser.
3. **Loopback only.** Bind `127.0.0.1` for ports `18100` / `18110` / `18111`.
4. **Headful Chromium** under Xvfb (`headless=False`). Headless trips detection.
5. **Same profile dir across restarts** or Cloudflare re-challenges every boot.
6. **No secrets in the repo.** Session state lives under `~/.chatgpt-adapter/`.

---

## 1. Repo layout (already present)

Flat package — open these files; do not regenerate them:

| File | Role |
|------|------|
| `browser.py` | `ChatGPTBrowser`: persistent Chromium + stealth + `save_state` |
| `gateway.py` | SPA driver HTTP server (`/status`, `/chat`, `/chat/stream`) |
| `adapter.py` | FastAPI OpenAI front + **delta protocol** |
| `session_inject.py` | Paste cookie header → profile + `cookies_parsed.json` |
| `login.py` | Optional login control daemon (`:18100`) |
| `run_gateway.sh` | Ensure Xvfb `:99`, then `gateway.py 18110` |
| `run_adapter.sh` | `uvicorn adapter:app --host 127.0.0.1 --port 18111` |
| `requirements.txt` | Pins below |
| `.gitignore` | Ignore session artifacts (see §8) |
| `LICENSE` | MIT |
| `README.md` | Human install/API docs |
| `AGENT_BUILD.md` | This contract |

Only create a file if it is actually missing from the tree.

### `requirements.txt`

```
fastapi==0.103.2
uvicorn==0.23.2
httpx==0.28.1
playwright==1.61.0
playwright-stealth==2.0.3
```

Setup (once per machine): `python3 -m venv .venv`,  
`pip install -r requirements.txt`, `python -m playwright install chromium`.  
System package: `xvfb` (+ `xdpyinfo`).

---

## 2. Runtime paths & env

Default home: `~/.chatgpt-adapter/` (`CHATGPT_HOME`).

| Path | Writer | Reader | Purpose |
|------|--------|--------|---------|
| `$CHATGPT_HOME/profile/` | Playwright | Playwright | Persistent Chromium profile (CF clearance) |
| `$CHATGPT_HOME/cookies_parsed.json` | `session_inject.py` | `gateway.py` boot | `[[name, value], ...]` cookie map |
| `$CHATGPT_HOME/state.json` | `browser.save_state` | ops | Token/cookie snapshot (chmod `600`) |
| `$CHATGPT_HOME/storage_state.json` | `session_inject.py` | ops | Playwright `storage_state()` dump |
| `$CHATGPT_HOME/session_info.json` | `session_inject.py` | ops | Login probe |
| `$CHATGPT_HOME/latest.png` | `browser.shot` / login | ops | Screenshot |

**Env vars:**

| Var | Default | Meaning |
|-----|---------|---------|
| `CHATGPT_HOME` | `~/.chatgpt-adapter` | Runtime state root |
| `CHATGPT_COOKIE_FILE` | (resolved) | Override cookie map path |
| `CHATGPT_TZ` | `Asia/Kolkata` | Chromium `timezone_id` |
| `CHATGPT_GATEWAY` | `http://127.0.0.1:18110` | Adapter → gateway URL |
| `DISPLAY` | `:99` | Set by browser/scripts |

**Cookie file resolution in gateway** (first hit wins):

1. `CHATGPT_COOKIE_FILE` if set  
2. `$CHATGPT_HOME/cookies_parsed.json` if it exists  
3. Legacy `/tmp/cookies_parsed.json` if it exists  
4. Else `$CHATGPT_HOME/cookies_parsed.json` (expected path; boot fails if missing)

---

## 3. `browser.py`

```python
BASE = expanduser(CHATGPT_HOME or "~/.chatgpt-adapter")
PROFILE = BASE/profile
STATE = BASE/state.json
TIMEZONE = CHATGPT_TZ or "Asia/Kolkata"
```

**`ChatGPTBrowser.start()`:**

- `sync_playwright().start()`
- `chromium.launch_persistent_context(user_data_dir=PROFILE, headless=False,
  viewport={1280,800}, locale="en-US", timezone_id=TIMEZONE, args=LAUNCH_ARGS)`
- Launch args exactly:
  `--no-sandbox --disable-gpu --use-angle=swiftshader --disable-dev-shm-usage
  --disable-blink-features=AutomationControlled --window-size=1280,900`
- Init script stealth: `navigator.webdriver` undefined; `languages=['en-US','en']`;
  fake `plugins`; `window.chrome={runtime:{}}`
- Also try `playwright_stealth.Stealth().apply_stealth_sync(page)` (try/except)
- Reuse `context.pages[0]` or `new_page()`

**Helpers:** `shot()` → `$BASE/latest.png` (fix: use `BASE`, not an undefined name);  
`localStorage()` → JSON of all keys;  
`save_state()` → write `state.json` chmod `600` with `saved_at`, `url`,
`localStorage`, `cookies` for `https://chatgpt.com`.

**Hard rules:** never `headless=True`; keep the same profile; warm with
`goto("https://www.google.com/")` before protected navigations.

---

## 4. Session inject (`session_inject.py`)

Password login from a datacenter IP usually fails device/proof checks. Normal
path = paste cookies from a logged-in real browser.

**Input file JSON:**

```json
{"token": "", "cookies": "name=value; name2=value2; ..."}
```

User source: DevTools → Network → any chatgpt.com request → copy full
`cookie:` request header value into that JSON.

**`session_inject.py` must:**

1. Parse `;`-separated cookies → `{name: value}`.
2. Write `$CHATGPT_HOME/cookies_parsed.json` as `[[name, value], ...]` (chmod `600`).
   **This file is required for gateway boot** — do not skip it.
3. Start `ChatGPTBrowser`, `add_cookies` for each cookie × domains
   `[".chatgpt.com", "chatgpt.com", ".openai.com", ".auth.openai.com"]`,
   `secure=True` if name contains `__Secure` or is `_dd_s`, else `False`;
   `sameSite="Lax"`, `path="/"`. Ignore per-domain failures.
4. Warm `google.com` → `chatgpt.com`; wait out CF interstitial up to ~4 min
   (sleep **before** reading `page.title()`; break when title lacks
   `just a moment` / `security verification`).
5. Settle ~8s; harvest:
   - `storage_state.json` from `ctx.storage_state()`
   - `session_info.json` with url/title/token probe
     (`localStorage` keys `accessToken`, `access_token`, `token`, `auth_token`)
     and `logged_in` = url has `chatgpt.com` and body does not start with "log in"
6. `b.stop()`.

**Usage:**

```bash
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
DISPLAY=:99 .venv/bin/python session_inject.py /path/to/cookies.json
```

---

## 5. Gateway (`gateway.py`) — contract

Print `COOKIE_FILE <path>` then boot; on success print `GATEWAY_UP <port>`.

### Boot

1. Load cookie pairs from resolved cookie file.
2. `ChatGPTBrowser().start()`; keep browser/page on `_state`.
3. For each `[name, value]`:
   - `secure` if name in  
     `{__Secure-next-auth.session-token.0, __Secure-next-auth.session-token.1,
       __Secure-oai-is, __Secure-next-auth.callback-url, __Host-next-auth.csrf-token,
       cf_clearance, __cf_bm, _cfuvid}`
   - domains: `["chatgpt.com"]` only if name in  
     `{__Host-next-auth.csrf-token, __Secure-next-auth.callback-url}`;  
     else all four chatgpt/openai domains. Ignore add failures.
4. `page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=120000)`
5. CF wait ≤420s, sleep **15s before each title read**; break when title nonempty
   and lacks `just a moment` / `security`.
6. Settle 8s; close welcome modal if visible:
   `[data-testid="close-button"]` then `button:has-text('Close')`.
7. `_state["ok"]=True`. Serve forever on `127.0.0.1:18110`.

### HTTP API

| Method | Path | Behavior |
|--------|------|----------|
| GET | `/status` | `{"ok", "title", "error", "turns"}` — `turns` = assistant message count in DOM |
| POST | `/chat` | Body `{"prompt","model?","reset?"}` → drain stream → `{"text"}` or 500 `{"error"}` |
| POST | `/chat/stream` | Same body → SSE `data: {json}\n\n` events |
| * | else | 404; bad JSON 400; not booted 503 |

SSE events: `{"delta","text"}` growth, final `{"done": true, "text": ...}`,
or `{"error": "..."}`.

### `ask_stream(page, prompt, model=None, reset=False)` — the core

1. Acquire global `threading.Lock` for the whole turn.
2. If `reset`: click `[data-testid="create-new-chat-button"]` (6s, swallow errors, sleep 0.8s).
3. `turns0` = count of `[data-message-author-role="assistant"]`.
4. Click `#prompt-textarea` (30s). Missing → yield error.
5. Insert via `document.execCommand('insertText', false, txt)` on `#prompt-textarea`.
   Must verify insert succeeded; fallback `page.keyboard.type(prompt, delay=15)`.
6. Sleep 0.3s; `keyboard.press("Enter")`.
7. Poll every 0.25s for ≤480s:
   - Read all assistant `innerText`s as JSON array.
   - If `len > turns0`: take `arr[turns0]`; on growth emit
     `{"delta": growth, "text": current}` (growth = suffix if prefix-stable).
   - Done when: turns > turns0 AND stop button
     (`[data-testid="stop-button"]`) not visible (`offsetParent` null) AND
     send button not disabled AND idle ≥ 3 polls.
   - Idle cap 45 → emit what you have.
8. Yield `{"done": true, "text": last}`.

Non-stream `ask` = drain the generator.

---

## 6. Adapter (`adapter.py`) — OpenAI front

FastAPI app title `chatgpt-web-adapter`. Gateway URL from `CHATGPT_GATEWAY`.

| Route | Response |
|-------|----------|
| `GET /v1/models` | `{"object":"list","data":[{"id":"gpt-5.6-luna","object":"model","owned_by":"openai"}]}` — keep id in sync with SPA label |
| `GET /health` | `{"ok": true, "gateway": <status json>}` |
| `POST /v1/chat/completions` | OpenAI chat completion (stream or not) |

### Prompt build

For each message:

- `system` → `[system] {content}`
- else → `[{role}] {content}`

Join with `\n`.

### Delta protocol (required)

Keep global `_prev_prompt`.

- If `_prev_prompt` is set AND `prompt.startswith(_prev_prompt)` AND longer  
  → gateway body `{"prompt": prompt[len(_prev_prompt):], "model", "reset": false}`
- Else → `{"prompt": prompt, "model", "reset": true}`
- Always set `_prev_prompt = prompt` after deciding.

This is what makes multi-turn agent clients cheap: the SPA keeps the thread and
caches the large prefix server-side.

### Streaming translation

- Backend: `httpx.stream POST {GATEWAY}/chat/stream`, timeout 600; parse `data:` lines.
- First event failure → 502 OpenAI-style `{"error":{"message","type":"backend_error"}}`.
- `stream=true` → OpenAI `chat.completion.chunk` SSE; `finish_reason:"stop"` on done; end with `data: [DONE]`.
- `stream=false` → one `chat.completion` with `usage: null`.

---

## 7. `login.py` (optional)

Control daemon on `127.0.0.1:18100` for assisted password login when cookies
cannot be pasted. Import `ChatGPTBrowser` from **this package dir** (not a
hardcoded absolute path). Screenshots → `$CHATGPT_HOME/latest.png`.

On start: warm google → open
`https://chatgpt.com/auth/login?screen_hint=password`.  
Print `LOGIN_DAEMON_READY ...`.

`POST /ctrl` JSON `{"op": ...}` ops: `goto`, `shot`, `eval`, `click`, `fill`,
`press`, `ck`, `waitclear`, `waitts`, `netlog`, `current`, `save`.  
Prefer cookie inject for production; this is a fallback.

---

## 8. Launchers, ignore, license

**`run_gateway.sh`:**

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/tmp/xvfb99.log 2>&1 &
  sleep 2
fi
exec .venv/bin/python gateway.py 18110
```

**`run_adapter.sh`:**

```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn adapter:app --host 127.0.0.1 --port 18111
```

PM2 (esp. 6.x): always start the `.sh` wrappers (`exec`), not venv console
scripts directly. Example:

```bash
pm2 start run_gateway.sh --name chatgpt-gateway
pm2 start run_adapter.sh --name chatgpt-adapter
pm2 save
```

**`.gitignore` must ignore:** `__pycache__/`, `*.pyc`, `.venv/`, `state.json`,
`storage_state.json`, `session_info.json`, `cookies_parsed.json`, `latest.png`,
`pasted_session.json`, `.DS_Store`.

**License:** MIT.

---

## 9. Bring-up order (run existing code)

Do not rewrite sources for a normal bring-up. From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium

# 1) inject session (writes cookies_parsed.json + profile)
DISPLAY=:99 .venv/bin/python session_inject.py ./cookies.json

# 2) gateway first, then adapter
.venv/bin/python gateway.py 18110
.venv/bin/python -m uvicorn adapter:app --host 127.0.0.1 --port 18111
```

Or: `pm2 start run_gateway.sh` / `run_adapter.sh` as in the README.

---

## 10. Acceptance tests (pass in order)

1. `GET :18110/status` → `ok: true`, `title` contains `ChatGPT`
2. `GET :18111/v1/models` → includes `gpt-5.6-luna` (or current SPA label)
3. `GET :18111/health` → `ok` and gateway ok
4. Non-stream completion: messages `[{"role":"user","content":"Reply exactly: PONG"}]`  
   → content ≈ `pong`, `finish_reason: stop`, within ~60s
5. Thread reuse: after (4), `/status` `turns == 1`. Send a **prefix-extending**
   second turn (same conversation growth). `turns` becomes `2` (no reset).
   A diverging full prompt starts a new chat (`reset: true`).
6. `stream: true` → chunk SSE + `data: [DONE]`
7. Restart both processes; `/status` recovers using the **same** profile dir

---

## 11. Anti-patterns (do not do)

- Raw API/Bearer conversation calls
- Threading Playwright / multi-worker gateway
- Reading `page.title()` before sleeping in CF loops
- Guessing model ids ahead of the SPA
- Committing cookies, `state.json`, profile, or screenshots
- Binding services on `0.0.0.0`
- Skipping `cookies_parsed.json` write in `session_inject.py`
- Hardcoding machine paths like `/home/ubuntu/...`

---

## 12. Expected performance notes

- Cold turn ~20s; reused-thread follow-up ~16s (grows more valuable with large context).
- Near context limit the SPA may auto-new-chat; `turns0` still captures the new
  assistant bubble; force reset with `reset: true` / diverging prompt when needed.
- One request at a time by design. Two interleaved clients each get resets.

---

## Done when

Your change preserves §0–§6 contracts, §10 tests still pass (or you documented
SPA-driven selector updates), no secrets landed in git, and ports stay
loopback-only. Prefer editing the existing modules over adding parallel
implementations.
