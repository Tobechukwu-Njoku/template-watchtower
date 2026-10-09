#!/usr/bin/env python3
"""Create the team's GitHub Apps from team/config.json, one browser form each.

  scripts/create-apps.py                # every App that has no credentials yet
  scripts/create-apps.py implementer    # one App, by its key in config.json
  scripts/create-apps.py --org ORG      # Apps owned by an organisation instead of you

For each App this opens GitHub's "Create GitHub App" page with the name and
permissions already filled in. You confirm it, GitHub sends you back here, and the
App's ID and private key are saved to ~/.watchtower/apps/ (key readable by you
only). Then install the App on the repositories the team should work in; the page
for that opens next.

Create the Apps once. Every project made from the template reuses them: install
them on the new repository. Python 3.9+, standard library only, plus a `gh` login.
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "team" / "config.json"


def home() -> Path:
    return Path(os.environ.get("WATCHTOWER_HOME", Path.home() / ".watchtower")) / "apps"


def manifest(app: dict, homepage: str, redirect: str) -> dict:
    return {
        "name": app["slug"],
        "url": homepage,
        "description": app["description"],
        "public": False,  # installable only on the owner's own account
        "hook_attributes": {"url": "https://example.com/watchtower-uses-no-webhooks", "active": False},
        "redirect_url": redirect,
        "default_permissions": app["permissions"],
        "default_events": [],
    }


def form_page(m: dict, state: str, org: str | None) -> str:
    action = (f"https://github.com/organizations/{urllib.parse.quote(org)}/settings/apps/new" if org
              else "https://github.com/settings/apps/new") + f"?state={state}"
    return f"""<!doctype html><meta charset="utf-8"><title>Create {html.escape(m['name'])}</title>
<body style="font:16px system-ui;margin:3em">
<form id="f" method="post" action="{html.escape(action)}">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(m))}">
<p>Taking you to GitHub to create <b>{html.escape(m['name'])}</b>.</p>
<button>Continue to GitHub</button></form>
<script>document.getElementById("f").submit()</script>"""


def wait_for_code(page_for, state: str, open_url=webbrowser.open, timeout: int = 900) -> str:
    """Serve the form on 127.0.0.1, open it, and wait for GitHub to redirect back with a code.
    `page_for(port)` returns the form page; the port is only known once the server is up."""
    got: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(u.query)
            if u.path == "/":
                self._send(200, page_for(self.server.server_port))
            elif u.path == "/callback" and q.get("state", [""])[0] == state and q.get("code"):
                got["code"] = q["code"][0]
                self._send(200, "<p style='font:16px system-ui;margin:3em'>Created. You can close this tab.</p>")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._send(400, "unexpected request")

        def _send(self, status: int, body: str):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    timer = threading.Timer(timeout, srv.shutdown)
    timer.start()
    url = f"http://127.0.0.1:{srv.server_port}/"
    print(f"   opening {url} - if no browser appears, open it yourself")
    threading.Thread(target=open_url, args=(url,), daemon=True).start()
    srv.serve_forever()
    timer.cancel()
    srv.server_close()
    if "code" not in got:
        sys.exit("   no answer from GitHub within 15 minutes - run this again")
    return got["code"]


def gh_json(*args: str):
    out = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:300])
    return json.loads(out.stdout)


def convert(code: str) -> dict:
    """Swap the one-time code for the App's ID and private key (valid for one hour)."""
    return gh_json("-X", "POST", f"/app-manifests/{code}/conversions")


def save(app: dict, keys: Path) -> Path:
    keys.mkdir(parents=True, exist_ok=True)
    os.chmod(keys, 0o700)
    pem = keys / f"{app['slug']}.pem"
    fd = os.open(pem, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(app["pem"])
    try:  # the bot's user id, needed later so commits show as the App
        bot_id = gh_json(f"/users/{urllib.parse.quote(app['slug'] + '[bot]')}")["id"]
    except (RuntimeError, KeyError):
        bot_id = None
    meta = {"id": app["id"], "slug": app["slug"], "client_id": app.get("client_id"),
            "html_url": app.get("html_url"), "bot_user_id": bot_id}
    (keys / f"{app['slug']}.json").write_text(json.dumps(meta, indent=2) + "\n")
    return pem


def set_slug(key: str, old: str, new: str) -> None:
    """GitHub may give a different slug (for example when the name was taken and you
    changed it). The gate trusts the slug in config.json, so it has to match."""
    text = CONFIG.read_text()
    needle = f'"slug": "{old}"'
    if text.count(needle) == 1:
        CONFIG.write_text(text.replace(needle, f'"slug": "{new}"'))
        print(f"   ! updated team/config.json: {key} slug {old} -> {new}. Commit that change.")
    else:
        print(f"   ! set apps.{key}.slug to {new!r} in team/config.json and commit it")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("apps", nargs="*", help="App keys from config.json (default: all without credentials)")
    ap.add_argument("--org", help="create the Apps under this organisation")
    ap.add_argument("--force", action="store_true", help="create even if credentials already exist")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text())
    keys = home()
    wanted = args.apps or list(cfg["apps"])
    unknown = [k for k in wanted if k not in cfg["apps"]]
    if unknown:
        sys.exit(f"unknown App {unknown}; choose from {list(cfg['apps'])}")
    remote = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True, text=True).stdout
    homepage = "https://github.com/" + remote.strip().split("github.com", 1)[-1].lstrip(":/").removesuffix(".git") \
        if "github.com" in remote else "https://github.com"

    for key in wanted:
        app = cfg["apps"][key]
        if (keys / f"{app['slug']}.json").exists() and not args.force:
            print(f"== {app['slug']}: already created (credentials in {keys}); --force to make another")
            continue
        print(f"== {app['slug']}: confirm the form on GitHub")
        state = secrets.token_urlsafe(16)
        code = wait_for_code(lambda port: form_page(manifest(app, homepage, f"http://127.0.0.1:{port}/callback"),
                                                    state, args.org), state)
        made = convert(code)
        pem = save(made, keys)
        print(f"   created {made['slug']} (App ID {made['id']}); private key saved to {pem}")
        if made["slug"] != app["slug"]:
            set_slug(key, app["slug"], made["slug"])
        install = f"https://github.com/apps/{made['slug']}/installations/new"
        print(f"   now install it on your repositories: {install}")
        webbrowser.open(install)
    return 0


if __name__ == "__main__":
    sys.exit(main())
