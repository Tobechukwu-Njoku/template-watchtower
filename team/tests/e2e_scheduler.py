#!/usr/bin/env python3
"""End-to-end: scripts/watch.sh for a reviewer and for Tim, with fake GitHub (App API and
gh), the fake connector and a stand-in Claude Code. Run: python3 team/tests/e2e_scheduler.py"""
import base64, http.server, json, os, subprocess, sys, tempfile, threading
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]; HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "team")); import team as T  # noqa: E402
tmp = Path(tempfile.mkdtemp()); HOME = tmp / "home"; STATE = tmp / "state.json"; CLAUDE_LOG = tmp / "claude.json"
REPO = T.GH._detect()
fails = 0
def check(name, cond):
    global fails; print(("ok   " if cond else "FAIL ") + name); fails += 0 if cond else 1

# App credentials for both Apps, and a fake GitHub App API that checks their signatures
(HOME / "apps").mkdir(parents=True)
pubs = {}
for key, app in T.CFG["apps"].items():
    pem = HOME / "apps" / f"{app['slug']}.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(pem), "2048"], capture_output=True, check=True)
    pub = tmp / f"{app['slug']}.pub"; subprocess.run(["openssl", "rsa", "-in", str(pem), "-pubout", "-out", str(pub)], capture_output=True, check=True)
    pubs[f"Iv1.{key}"] = (pub, app["slug"])
    (HOME / "apps" / f"{app['slug']}.json").write_text(json.dumps({"id": 1, "slug": app["slug"], "client_id": f"Iv1.{key}", "bot_user_id": 99}))
unb = lambda x: base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
class API(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _app(self):
        h, b, s = self.headers["Authorization"].split()[1].split(".")
        pub, slug = pubs[json.loads(unb(b))["iss"]]
        (tmp / "sig").write_bytes(unb(s))
        ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pub), "-signature", str(tmp / "sig")], input=f"{h}.{b}".encode(), capture_output=True).returncode == 0
        return slug if ok else None
    def _reply(self, code, obj):
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(obj).encode())
    def do_GET(self): self._reply(200, {"id": 7}) if self._app() else self._reply(401, {})
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        slug = self._app(); self._reply(201, {"token": f"ghs_for_{slug}"}) if slug else self._reply(401, {})
srv = http.server.HTTPServer(("127.0.0.1", 0), API); threading.Thread(target=srv.serve_forever, daemon=True).start()

# Fake GitHub data: a PR from Tim awaiting review, an issue the maintainer marked ready
TIM, REV, A = T.app_login("tim-drake"), T.app_login("barbara-gordon"), "a" * 40
diff = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1,1 +1,2 @@\n a = 1\n+b = 2\n"
state = {"repo": REPO, "clock": "2026-10-09T10:00:00Z", "next_id": 100, "pushed_at": "2026-10-09T09:59:00Z", "statuses": [],
  "identities": {"reviewers": {"login": REV, "type": "Bot", "association": "NONE"}, "tim": {"login": TIM, "type": "Bot", "association": "NONE"}},
  "issues": {"5": {"number": 5, "title": "feat: b", "state": "open", "body": "", "labels": [], "user": {"login": TIM, "type": "Bot"},
                   "author_association": "NONE", "pull_request": {}, "created_at": "2026-10-09T09:00:00Z", "updated_at": "2026-10-09T09:59:00Z", "html_url": "u5"},
             "7": {"number": 7, "title": "Add export", "state": "open", "body": "Please add export.", "labels": [{"name": "ready"}],
                   "user": {"login": "owner", "type": "User"}, "author_association": "OWNER", "created_at": "2026-10-09T09:00:00Z",
                   "updated_at": "2026-10-09T09:00:00Z", "html_url": "u7"}},
  "pulls": {"5": {"number": 5, "draft": False, "title": "feat: b", "body": "Adds b.", "head": {"sha": A}, "user": {"login": TIM, "type": "Bot"},
                  "author_association": "NONE", "diff": diff, "files": [{"filename": "src/x.py"}], "commits": [{"commit": {"message": "feat: b"}}]}},
  "comments": {"5": [{"id": 1, "body": T.marker("request", id="james-gordon", sha=A[:12]) + "\nTo: reviewers", "user": {"login": T.BOT, "type": "Bot"},
                      "author_association": "NONE", "created_at": "2026-10-09T09:59:00Z", "updated_at": "2026-10-09T09:59:00Z", "html_url": "c1"}]},
  "events": {"7": [{"event": "labeled", "label": {"name": "ready"}, "actor": {"login": "owner", "type": "User"}}]}}
STATE.write_text(json.dumps(state))
gdir = HOME / "google" / "google-1" / ".gemini" / "antigravity-acp"; gdir.mkdir(parents=True); (gdir / "acp_token.json").write_text("{}")
(HOME / "claude-token").write_text("sk-ant-oat-fake\n")
origin = tmp / "origin.git"; subprocess.run(["git", "clone", "-q", "--bare", str(ROOT), str(origin)], check=True)

def watch(member, who):
    env = dict(os.environ, PATH=f"{HERE / 'fakegh'}:{HERE / 'fakeclaude'}:{os.environ['PATH']}", WATCHTOWER_HOME=str(HOME),
               GITHUB_API_URL=f"http://127.0.0.1:{srv.server_port}", FAKE_GH_STATE=str(STATE), FAKE_GH_AS=who,
               WATCHTOWER_ACP_SERVER=str(ROOT / "team/tests/fake_acp.py"), FAKE_ACP_REPLIES=json.dumps(['{"summary": "Fine.", "findings": []}']),
               WATCHTOWER_CLONE_URL=str(origin), FAKE_CLAUDE_LOG=str(CLAUDE_LOG), CLAUDE_CODE_SIMPLE="1", ANTHROPIC_BASE_URL="http://desktop-proxy")
    env["GITHUB_REPOSITORY"] = REPO  # a real clone reads it from its github.com remote; this one is cloned from a local folder
    r = subprocess.run(["/bin/bash", str(ROOT / "scripts/watch.sh"), member], env=env, capture_output=True, text=True)
    logs = "".join(p.read_text() for p in (HOME / "logs").glob(f"*-{member}.log"))
    return r, logs

st = lambda: json.loads(STATE.read_text())
r, log = watch("barbara-gordon", "reviewers")
check("reviewer check exits cleanly", r.returncode == 0)
check("Barbara's review was posted by the reviewers' App", any("team:review id=barbara-gordon" in c["body"] and c["user"]["login"] == REV for c in st()["comments"]["5"]))
check("her log records the run", "Approved" in log and "connector" in log)
r, log = watch("barbara-gordon", "reviewers")
check("a second check within 10 minutes sleeps (schedule applied)", log.count("== ") == 1)

r, log = watch("tim-drake", "tim")
rec = json.loads(CLAUDE_LOG.read_text())
clone = HOME / "clones" / REPO.replace("/", "__") / "tim-drake"
check("Tim was started in his own clone", rec["cwd"] == str(clone.resolve()) or rec["cwd"] == str(clone))
check("with his App's token and marked as an agent", rec["env"]["GH_TOKEN"] == f"ghs_for_{T.CFG['apps']['implementer']['slug']}" and rec["env"]["WATCHTOWER_AGENT"] == "tim-drake")
check("with the saved Claude login, and none of the inherited Claude session settings",
      rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-fake" and rec["env"]["CLAUDE_CODE_SIMPLE"] is None and rec["env"]["ANTHROPIC_BASE_URL"] is None)
argv = rec["argv"]
check("in dontAsk mode, project settings only, no transcript, on the configured model",
      argv[argv.index("--permission-mode") + 1] == "dontAsk" and argv[argv.index("--setting-sources") + 1] == "project"
      and "--no-session-persistence" in argv and argv[argv.index("--model") + 1] == T.CFG["implementing"]["model"])
check("Tim's claim was posted by his App", any(c["user"]["login"] == TIM and "Picked up" in c["body"] for c in st()["comments"].get("7", [])))
tok = (tmp / "claude.json.token").read_text()
check("Tim cannot mint a reviewer's token", not tok.startswith("0") and "not for a running agent" in tok)

print(f"\n{'all passed' if not fails else f'{fails} failed'}"); sys.exit(1 if fails else 0)
