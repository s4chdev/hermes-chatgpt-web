#!/usr/bin/env python3
"""OpenAI-compatible API for the local ChatGPT web session (general chat quota).

Serves /v1/models, /v1/chat/completions (SSE + non-stream) on localhost.
Backend: the SPA gateway (gateway.py) which drives the real chatgpt.com UI
with the injected browser session, so OpenAI's device/proof checks pass and the
GENERAL chat subscription quota is consumed (not codex).
"""
import json
import os
import time
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

BASE = os.path.dirname(os.path.abspath(__file__))
GATEWAY = os.environ.get("CHATGPT_GATEWAY", "http://127.0.0.1:18110")

# Full prompt sent for the previous turn. When the next full prompt starts with
# this prefix (Hermes always replays the whole conversation), only the delta is
# sent and the SAME chatgpt.com thread is continued, so the ~30k context prefix
# stays cached server-side. Any divergence triggers a fresh chat with the full
# prompt.
_prev_prompt = None

app = FastAPI(title="chatgpt-web-adapter")

_MODELS = [
    {"id": "gpt-5.6-luna", "object": "model", "owned_by": "openai"},
]


def _gw_status():
    try:
        r = httpx.get(f"{GATEWAY}/status", timeout=5)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def _gw_chat_stream(body: dict):
    """Yield gateway SSE events (dicts) for one chat turn."""
    with httpx.stream("POST", f"{GATEWAY}/chat/stream",
                      json=body, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            try:
                yield json.loads(data)
            except Exception:
                continue


def _chunk(cid: str, created: int, model: str, delta: str, done: bool) -> str:
    payload = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0,
                     "delta": {"content": delta} if delta else {},
                     "finish_reason": "stop" if done else None}],
    }
    return "data: " + json.dumps(payload) + "\n\n"


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": _MODELS}


@app.get("/health")
async def health():
    return {"ok": True, "gateway": _gw_status()}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "invalid JSON body", "type": "invalid_request_error"}}, status_code=400)

    messages = body.get("messages") or []
    if not messages:
        return JSONResponse({"error": {"message": "messages required", "type": "invalid_request_error"}}, status_code=400)

    stream = bool(body.get("stream", False))
    model = body.get("model") or "auto"

    # strip the system message (chatgpt web has no system role; merge into prompt)
    user_parts = []
    for m in messages:
        if m.get("role") == "system":
            user_parts.append(f"[system] {m.get('content', '')}")
        else:
            user_parts.append(f"[{m.get('role', 'user')}] {m.get('content', '')}")
    prompt = "\n".join(user_parts)

    # Delta protocol: reuse the live thread when the full conversation is just
    # a prefix of what we sent before (normal Hermes turn growth). Only the
    # appended text goes to the gateway, with reset=False so the SAME chatgpt.com
    # conversation continues and the big context prefix is served from cache.
    # On any divergence (new session, changed system prompt, cache cold start)
    # send the FULL prompt to a FRESH chat.
    global _prev_prompt
    if _prev_prompt is not None and prompt.startswith(_prev_prompt) and len(prompt) > len(_prev_prompt):
        body = {"prompt": prompt[len(_prev_prompt):], "model": model, "reset": False}
    else:
        body = {"prompt": prompt, "model": model, "reset": True}
    _prev_prompt = prompt

    # gateway is single-flight; stream from the SPA, proxied as SSE
    try:
        gw_iter = _gw_chat_stream(body)
        first = next(gw_iter)  # raises on gateway/HTTP errors
    except StopIteration:
        return JSONResponse({"error": {"message": "gateway returned no events", "type": "backend_error"}}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": {"message": f"gateway error: {e}", "type": "backend_error"}}, status_code=502)

    if first.get("error"):
        return JSONResponse({"error": {"message": first["error"], "type": "backend_error"}}, status_code=502)
    cid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def gen():
        if stream:
            # first event may already carry a delta; emit it before the loop
            if first.get("delta") is not None or first.get("done"):
                yield _chunk(cid, created, model, first.get("delta", ""), first.get("done") is not None)
            for ev in gw_iter:
                if ev.get("error"):
                    yield f"data: {json.dumps({'error': {'message': ev['error'], 'type': 'backend_error'}})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                yield _chunk(cid, created, model, ev.get("delta", ""), ev.get("done") is not None)
            yield "data: [DONE]\n\n"
        else:
            # accumulate deltas, emit a single completion object
            text = first.get("text") or first.get("delta") or ""
            for ev in gw_iter:
                if ev.get("error"):
                    return
                if ev.get("done"):
                    text = ev.get("text", text)
                elif ev.get("text"):
                    text = ev["text"]
            yield json.dumps({
                "id": cid,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": None,
            })

    return StreamingResponse(gen(), media_type="text/event-stream" if stream else "application/json")