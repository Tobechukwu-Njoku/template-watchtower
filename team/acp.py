"""Talks to Gemini through Google Antigravity's ACP server, the connector T3 Code
installs (ACP is the Agent Client Protocol: JSON-RPC over the server's stdin and
stdout). Used for the reviewers: one prompt in, one reply out, no tools.

Each Google account gets its own home folder, so its login and its saved
conversations stay apart from every other account and from your own Antigravity.
The server runs in an empty folder, every tool request is refused, and each
session's saved conversation is deleted once the server exits.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

PROTOCOL = 1


class AcpError(RuntimeError):
    pass


class NeedsLogin(AcpError):
    pass


def find_installed_connector() -> dict | None:
    """The connector T3 Code currently uses, from its active.json. None if not installed."""
    for active in sorted(Path.home().glob(".t3/tools/antigravity-acp/*/active.json")):
        try:
            release = json.loads(active.read_text())["releaseId"]
        except (OSError, ValueError, KeyError):
            continue
        folder = active.parent / "versions" / release
        server = folder / "agy_acp_server.par"
        if server.exists():
            version = release[:12]
            try:
                version = json.loads((folder / ".install-complete.json").read_text()).get("version", version)
            except (OSError, ValueError):
                pass
            return {"path": str(server), "version": version}
    return None


class Connector:
    """One ACP server process. Use as a context manager; sessions opened through it are
    forgotten (their saved conversations deleted) when it closes."""

    def __init__(self, server: str, home: Path, on_stderr=None):
        self.server, self.home, self.on_stderr = server, Path(home), on_stderr
        self.sessions: list[str] = []
        self.denied: list[str] = []
        self._id = 0
        self._lines: queue.Queue = queue.Queue()
        self._err: list[str] = []

    # ------------------------------------------------------------------ process

    def __enter__(self):
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.workdir = tempfile.mkdtemp(prefix="watchtower-review-")
        # HOME, not GEMINI_HOME: with its own HOME the server keeps the login in a file in
        # that folder instead of a shared keychain entry, so accounts cannot overwrite each other.
        env = dict(os.environ, HOME=str(self.home))
        self.proc = subprocess.Popen([self.server], cwd=self.workdir, env=env, text=True, bufsize=1,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.call("initialize", {"protocolVersion": PROTOCOL, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=120)
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        shutil.rmtree(self.workdir, ignore_errors=True)
        for sid in self.sessions:
            forget(self.home, sid)
        return False

    def _pump_out(self):
        for line in self.proc.stdout:
            self._lines.put(line)

    def _pump_err(self):
        for line in self.proc.stderr:
            self._err = (self._err + [line])[-40:]
            if self.on_stderr:
                self.on_stderr(line)

    def _send(self, obj: dict) -> None:
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            raise AcpError(f"connector is not running: {''.join(self._err)[-600:]}") from None

    # ------------------------------------------------------------------ protocol

    def call(self, method: str, params: dict, timeout: float, on_update=None) -> dict:
        self._id += 1
        my = self._id
        self._send({"jsonrpc": "2.0", "id": my, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise AcpError(f"{method}: no answer within {int(timeout)}s")
            try:
                line = self._lines.get(timeout=min(left, 1))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AcpError(f"connector exited ({self.proc.returncode}): {''.join(self._err)[-600:]}")
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == my and "method" not in msg:
                if "error" in msg:
                    err = msg["error"]
                    text = f"{err.get('message')}: {json.dumps(err.get('data'))[:300]}"
                    if "auth" in str(err.get("message", "")).lower():
                        raise NeedsLogin(text)
                    raise AcpError(f"{method}: {text}")
                return msg.get("result") or {}
            self._handle(msg, on_update)

    def _handle(self, msg: dict, on_update) -> None:
        method = msg.get("method")
        if method == "session/update":
            if on_update:
                on_update(msg["params"].get("update", {}))
        elif "id" in msg and method == "session/request_permission":
            # Reviewers read; they never act. Refuse every tool, whatever it is.
            self.denied.append(str((msg["params"].get("toolCall") or {}).get("title", "a tool")))
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
        elif "id" in msg and method:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})

    def new_session(self) -> tuple[str, list]:
        """Open a session. Raises NeedsLogin if this account has not signed in."""
        res = self.call("session/new", {"cwd": self.workdir, "mcpServers": []}, timeout=120)
        self.sessions.append(res["sessionId"])
        return res["sessionId"], res.get("configOptions") or []

    def login(self, timeout: float = 600) -> None:
        """Start Google's browser sign-in for this account and wait for it to finish."""
        self.call("authenticate", {"methodId": "oauth-personal"}, timeout=timeout)

    def set_model(self, sid: str, model: str) -> None:
        res = self.call("session/set_config_option", {"sessionId": sid, "configId": "model", "value": model}, timeout=60)
        now = [o.get("currentValue") for o in res.get("configOptions", []) if o.get("id") == "model"]
        if now and now[0] != model:
            raise AcpError(f"asked for model {model}, connector is using {now[0]}")

    def prompt(self, sid: str, text: str, timeout: float) -> str:
        """Send one message and return the reply text."""
        chunks: list[str] = []

        def collect(update):
            if update.get("sessionUpdate") == "agent_message_chunk" and (update.get("content") or {}).get("type") == "text":
                chunks.append(update["content"]["text"])

        res = self.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
                        timeout=timeout, on_update=collect)
        if res.get("stopReason") not in (None, "end_turn"):
            raise AcpError(f"reply stopped early: {res.get('stopReason')}")
        return "".join(chunks)


def forget(home: Path, sid: str) -> None:
    """Delete a session's saved conversation (the connector stores each one in full)."""
    folder = Path(home) / ".gemini" / "antigravity-acp" / "conversations"
    for f in folder.glob(f"{sid}.*"):
        f.unlink(missing_ok=True)


def model_choices(config_options: list) -> list[str]:
    return [o["value"] for opt in config_options if opt.get("id") == "model" for o in opt.get("options", [])]
