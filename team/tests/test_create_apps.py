#!/usr/bin/env python3
"""scripts/create-apps.py against a stand-in for GitHub: the browser is a function and
the manifest conversion is stubbed. Restores team/config.json afterwards. No network."""
import html
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("create_apps", ROOT / "scripts" / "create-apps.py")
ca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ca)
cfg = json.loads(ca.CONFIG.read_text())
TIM, REVIEWERS = cfg["apps"]["implementer"]["slug"], cfg["apps"]["reviewers"]["slug"]
TAKEN = TIM + "-2"  # GitHub gives Tim's App a different name, as when the name is taken
tmp = Path(tempfile.mkdtemp())
os.environ["WATCHTOWER_HOME"] = str(tmp)
seen = {}


def fake_browser(url):
    if "installations/new" in url:
        seen.setdefault("install", []).append(url)
        return
    page = urllib.request.urlopen(url).read().decode()
    manifest = json.loads(html.unescape(re.search(r'name="manifest" value="([^"]+)"', page).group(1)))
    action = html.unescape(re.search(r'action="([^"]+)"', page).group(1))
    seen.setdefault("manifests", []).append((manifest, action))
    state = action.split("state=")[1]
    base = url.rstrip("/")
    try:
        urllib.request.urlopen(f"{base}/callback?code=x&state=WRONG")
        seen["wrong_state"] = "accepted"
    except urllib.error.HTTPError as e:
        seen["wrong_state"] = e.code
    urllib.request.urlopen(f"{base}/callback?code=CODE-{manifest['name']}&state={state}")


def fake_gh_json(*args):
    if args[:2] == ("-X", "POST"):
        slug = args[2].split("/")[2][len("CODE-"):]
        slug = TAKEN if slug == TIM else slug
        return {"id": 101, "slug": slug, "client_id": "Iv1.abc", "html_url": "h", "pem": "-----KEY-----\n"}
    return {"id": 555}


ca.webbrowser.open = fake_browser
ca.wait_for_code.__defaults__ = (fake_browser, 900)  # the default argument captured the real browser
ca.gh_json = fake_gh_json
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += 0 if cond else 1


backup = ca.CONFIG.read_text()
try:
    sys.argv = ["create-apps.py"]
    ca.main()
    (m1, a1), (m2, a2) = seen["manifests"]
    check("Tim's App asks for no workflow permission", m1["name"] == TIM and "workflows" not in m1["default_permissions"], m1)
    check("the reviewers' App can only read code, and is private",
          m2["default_permissions"]["contents"] == "read" and m2["public"] is False, m2)
    check("GitHub sends you back to this machine, and no webhook is set up",
          m1["redirect_url"].startswith("http://127.0.0.1:") and m1["hook_attributes"]["active"] is False, m1)
    check("the form goes to your account's App settings", a1.startswith("https://github.com/settings/apps/new?state="), a1)
    check("a reply with the wrong state is refused", seen["wrong_state"] == 400, seen["wrong_state"])
    keys = tmp / "apps"
    pem = keys / f"{TAKEN}.pem"
    check("the private key is readable by you only",
          stat.S_IMODE(pem.stat().st_mode) == 0o600 and stat.S_IMODE(keys.stat().st_mode) == 0o700)
    meta = json.loads((keys / f"{REVIEWERS}.json").read_text())
    check("IDs are saved, the key is not copied into them",
          meta["client_id"] == "Iv1.abc" and meta["bot_user_id"] == 555 and "pem" not in meta, meta)
    check("a different name from GitHub is written back to config.json", f'"slug": "{TAKEN}"' in ca.CONFIG.read_text())
    check("the installation page opens for each App", len(seen["install"]) == 2, seen.get("install"))
    seen.clear()
    ca.main()
    check("a second run does nothing", "manifests" not in seen)
finally:
    ca.CONFIG.write_text(backup)
sys.exit(1 if fails else 0)
