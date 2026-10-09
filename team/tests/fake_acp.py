#!/usr/bin/env python3
"""A stand-in for Antigravity's ACP server, for team/selftest.py. Speaks just enough of
the protocol, and behaves like the real one where it matters to us: it needs a sign-in,
it asks to use a tool before answering, and it saves every conversation to disk.

Environment: FAKE_ACP_REPLIES (JSON list of replies, one per prompt), FAKE_ACP_LOG
(JSON lines of what happened), FAKE_ACP_HANG=1 (never answer a prompt)."""
import json
import os
import sys
import uuid
from pathlib import Path

base = Path(os.environ["HOME"]) / ".gemini" / "antigravity-acp"
token = base / "acp_token.json"
replies = json.loads(os.environ.get("FAKE_ACP_REPLIES", '["{}"]'))
models = ["gemini-pro-agent", "gemini-3.8-flash-high"]
current = {"model": "gemini-3.7-flash-high"}
prompts = 0


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def log(**kv):
    if os.environ.get("FAKE_ACP_LOG"):
        with open(os.environ["FAKE_ACP_LOG"], "a") as f:
            f.write(json.dumps(kv) + "\n")


def options():
    return [{"id": "model", "currentValue": current["model"], "options": [{"value": m} for m in models]}]


def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None


while True:
    msg = read()
    if msg is None:
        break
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "authMethods": [{"id": "oauth-personal"}]}})
    elif method == "authenticate":
        base.mkdir(parents=True, exist_ok=True)
        token.write_text("{}")
        log(event="login")
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif method == "session/new":
        if not token.exists():
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "Authentication required"}})
            continue
        sid = str(uuid.uuid4())
        conv = base / "conversations"
        conv.mkdir(parents=True, exist_ok=True)
        for ext in ("db", "db-wal", "meta"):
            (conv / f"{sid}.{ext}").write_text(params.get("cwd", ""))
        log(event="session", cwd=params.get("cwd"), cwd_empty=not any(Path(params["cwd"]).iterdir()))
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid, "configOptions": options()}})
    elif method == "session/set_config_option":
        if params.get("value") in models:
            current["model"] = params["value"]
        send({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": options()}})
    elif method == "session/prompt":
        if os.environ.get("FAKE_ACP_HANG"):
            continue
        send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission",
              "params": {"sessionId": params["sessionId"], "toolCall": {"title": "run_shell_command"}, "options": []}})
        answer = read()
        text = params["prompt"][0]["text"]
        log(event="prompt", model=current["model"], chars=len(text), text=text,
            tool_outcome=((answer or {}).get("result") or {}).get("outcome", {}).get("outcome"))
        reply = replies[min(prompts, len(replies) - 1)]
        prompts += 1
        for piece in (reply[: len(reply) // 2], reply[len(reply) // 2:]):
            send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": params["sessionId"],
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": piece}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "unknown method"}})
