# ChatGPT Web Adapter

An OpenAI-compatible API that serves LLM requests from a **real chatgpt.com
web session** instead of an expensive per-token API key. It drives the actual
web app with Playwright + Xvfb and scrapes the assistant's reply, so it uses
your account's **general web chat quota** (the same quota the website uses),
not the API quota.

No OpenAI API key needed. Works with any OpenAI-compatible client:
curl, OpenAI SDKs, agent frameworks, or your own scripts.

> **Disclaimer:** Not affiliated with OpenAI. Automating chatgpt.com may
> violate OpenAI's Terms of Use and can risk account restriction. Use only on
> accounts you own, at human-like rates, one request at a time. Keep your
> session cookies private. Not for resale or high-frequency abuse. Provided
> as-is under the MIT License with no warranty.

## How it works

```
Your client  (OpenAI-compatible)
   |
   |  POST /v1/chat/completions
   v
adapter.py   (FastAPI, port 18111)
   |
   |  HTTP /chat  /chat/stream  /status
   v
gateway.py   (single-threaded HTTP server, port 18110)
   |
   |  Playwright (sync API, persistent browser profile, Xvfb display)
   v
browser.py   (stealth Chromium, logged into chatgpt.com)
   |
   v
chatgpt.com web UI  (real composer, real conversation thread)
```

- **Gateway**: a persistent headful Chrome session holds your login. For
  each request it types into the composer, presses Enter, and scrapes the
  new assistant message, streaming text growth as deltas.
- **Adapter**: translates that into a standard
  `/v1/models` + `/v1/chat/completions` API (streaming SSE or one-shot).
- Everything binds to loopback only.

## Components

| File | Purpose |
|------|---------|
| `gateway.py` | Drives one persistent Chromium session on chatgpt.com; answers `/chat` and `/chat/stream`, exposes `/status` |
| `adapter.py` | FastAPI OpenAI-compatible front; merges messages, handles thread reuse, proxies to the gateway |
| `browser.py` | Browser launcher: persistent profile, stealth settings, state snapshot |
| `session_inject.py` | Refresh tool: paste a `cookie:` header from your browser and it re-logs this machine's profile |
| `login.py` | Optional manual-login control daemon for password-based sign-in |

### Runtime paths (defaults)

| Path | Role |
|------|------|
| `~/.chatgpt-adapter/profile/` | Persistent Chromium profile (Cloudflare clearance survives restarts) |
| `~/.chatgpt-adapter/cookies_parsed.json` | `[[name, value], ...]` cookie map loaded by the gateway at boot |
| `~/.chatgpt-adapter/state.json` | Token/cookie snapshot from `save_state` |
| `~/.chatgpt-adapter/storage_state.json` | Playwright storage state from `session_inject.py` |
| `~/.chatgpt-adapter/session_info.json` | Login probe output from `session_inject.py` |

Override with `CHATGPT_HOME`, `CHATGPT_COOKIE_FILE`, `CHATGPT_TZ`, or
`CHATGPT_GATEWAY`. The gateway also accepts a legacy `/tmp/cookies_parsed.json`
if the home path is missing (original deployment layout).

---

## Quick start

Prereqs: Python 3.10+, Playwright Chromium, Xvfb (Linux), and a ChatGPT
account with a working web session (any plan with web chat).

```bash
git clone https://github.com/<you>/chatgpt-web-adapter.git
cd chatgpt-web-adapter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

The adapter needs the session of an already-logged-in browser. Simplest
way: in a normal browser, open chatgpt.com, open DevTools -> Network ->
reload, click any request, copy the full `cookie:` request header, and
save a JSON file:

```json
{"token": "", "cookies": "__Secure-next-auth.session-token.0=...; ..."}
```

Then inject it into the machine's Chrome profile (this also writes
`~/.chatgpt-adapter/cookies_parsed.json` for the gateway):

```bash
Xvfb :99 -screen 0 1280x800x24 &
DISPLAY=:99 .venv/bin/python session_inject.py /path/to/cookies.json
```

Now run the two services (start the gateway first):

```bash
.venv/bin/python gateway.py 18110        # terminal 1
.venv/bin/python -m uvicorn adapter:app --host 127.0.0.1 --port 18111   # terminal 2
```

The gateway prints `GATEWAY_UP 18110` when the browser is in and the
Cloudflare interstitial clears (can take a few minutes on first run).

---

## API

### `GET http://127.0.0.1:18111/v1/models`
```json
{"object": "list", "data": [{"id": "gpt-5.6-luna", "object": "model", "owned_by": "openai"}]}
```

### `POST http://127.0.0.1:18111/v1/chat/completions`
Standard OpenAI body (`stream` optional; `model` is echoed back,
but the SPA's current default model does the work).

```bash
curl -s http://127.0.0.1:18111/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.6-luna","messages":[{"role":"user","content":"Say hi"}]}'
```

```json
{"id":"chatcmpl-...","object":"chat.completion","created":...,
 "model":"gpt-5.6-luna","choices":[{"index":0,
 "message":{"role":"assistant","content":"Hi!"},
 "finish_reason":"stop"}],"usage":null}
```

Streaming sends standard `chat.completion.chunk` SSE events and finishes
with `data: [DONE]`.

### `GET http://127.0.0.1:18110/status`
```json
{"ok": true, "title": "ChatGPT", "error": null, "turns": 1}
```
`turns` = assistant messages in the current thread (see below).

### `GET http://127.0.0.1:18111/health`
`{"ok": true, "gateway": {...}}`

---

## Feature: thread reuse (the "delta protocol")

Long conversations replay their whole history every turn. By default the
adapter detects when a new request is just the previous prompt plus new
text (normal conversation growth) and:

- sends **only the appended delta** to a fresh chat, keeping the SAME
  chatgpt.com thread alive. The big context is cached server-side, later
  turns only pay for new tokens.
- Any divergence (new session, changed system prompt, interleaved clients)
  automatically resets to a fresh conversation with the full prompt.

Effects in `/status`: `turns` increments by 1 per turn while a thread is
reused, and resets when a new chat starts. Measured on a small test:
turn 1 ~20s cold, turn 2 ~16s while reusing the thread; the gap grows
when the context is large.

---

## Running as a service

PM2 (uses `.sh` wrappers on some distros because of a PM2 6.x bun-fork
that refuses venv console scripts):

```bash
pm2 start run_gateway.sh --name chatgpt-gateway
pm2 start run_adapter.sh --name chatgpt-adapter
pm2 save
```

or a plain systemd unit per service with `Restart=always`. The gateway
startup script ensures Xvfb :99 is up first.

---

## Operations & troubleshooting

| Symptom | Fix |
|---------|-----|
| 403 "unusual activity" | You bypassed the SPA. Only the browser flow works. |
| "log in" content in the page | Session expired; re-inject cookies with `session_inject.py`. |
| Cloudflare interstitial loop | Keep the same profile dir; first-run can take a few minutes; don't read `page.title()` before sleeping, or it races. |
| Slow first reply | First turn after boot is cold; subsequent reused-thread turns are faster. |
| Stale/garbled replies | The SPA auto-started a new chat at max context; send a diverging prompt (delta protocol) to force a fresh conversation. |

## Limitations

- One request at a time (the browser session is single-threaded by design).
- Two concurrent clients diverge the thread each turn; each gets a new
  chat, serialized.
- The assistant message count is bounded by the web UI's context window.
- Model name is whatever chatgpt.com currently ships; mirror the SPA labels.
- Requires a valid web session cookie; sessions expire and must be
  re-injected periodically.

## For agents

Use **[AGENT_BUILD.md](AGENT_BUILD.md)** — behavioral contract for the
**existing** code (do not recreate the tree). Covers selectors, delta
protocol, paths, and acceptance tests for verify/fix/extend work.

## License

MIT — see [LICENSE](LICENSE).
