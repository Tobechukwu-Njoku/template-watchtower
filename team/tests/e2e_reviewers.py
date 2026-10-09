#!/usr/bin/env python3
"""End-to-end: a reviewer's check-in (`team.py run`) against a fake GitHub and the fake
Antigravity connector. Run: python3 team/tests/e2e_reviewers.py"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fakegh"
sys.path.insert(0, str(ROOT / "team"))
import team as T  # noqa: E402

tmp = Path(tempfile.mkdtemp()); STATE = tmp / "state.json"; HOME = tmp / "home"; LOG = tmp / "acp.log"
REPO, TIM, REV, A = "owner/project", T.app_login("tim-drake"), T.app_login("barbara-gordon"), "a" * 40
fails = 0
def check(name, cond):
    global fails; print(("ok   " if cond else "FAIL ") + name); fails += 0 if cond else 1

diff = ("diff --git a/src/auth.py b/src/auth.py\n--- a/src/auth.py\n+++ b/src/auth.py\n@@ -1,2 +1,3 @@\n"
        " import os\n+TOKEN = os.environ['TOKEN']\n print('ok')\n")
state = {"repo": REPO, "clock": "2026-10-08T10:00:00Z", "next_id": 100, "pushed_at": "2026-10-08T09:00:00Z", "statuses": [],
  "identities": {"ci": {"login": T.BOT, "type": "Bot", "association": "NONE"},
                 "reviewers": {"login": REV, "type": "Bot", "association": "NONE"},
                 "tim": {"login": TIM, "type": "Bot", "association": "NONE"}},
  "issues": {"5": {"number": 5, "title": "feat(auth): read the token", "state": "open", "body": "", "labels": [],
                   "user": {"login": TIM, "type": "Bot"}, "author_association": "NONE", "pull_request": {},
                   "created_at": "2026-10-08T09:00:00Z", "updated_at": "2026-10-08T09:00:00Z", "html_url": "u5"},
             "7": {"number": 7, "title": "How should secrets be loaded?", "state": "open", "body": "Opening question.", "labels": [],
                   "user": {"login": "owner", "type": "User"}, "author_association": "OWNER",
                   "created_at": "2026-10-08T09:00:00Z", "updated_at": "2026-10-08T09:00:00Z", "html_url": "u7"}},
  "pulls": {"5": {"number": 5, "draft": False, "title": "feat(auth): read the token", "head": {"sha": A},
                  "body": "Reads the token.\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
                  "user": {"login": TIM, "type": "Bot"}, "author_association": "NONE", "diff": diff,
                  "files": [{"filename": "src/auth.py"}], "commits": [{"commit": {"message": "feat(auth): read the token"}}]}},
  "comments": {}, "events": {}}
STATE.write_text(json.dumps(state))
token_dir = HOME / "google" / "google-1" / ".gemini" / "antigravity-acp"; token_dir.mkdir(parents=True); (token_dir / "acp_token.json").write_text("{}")
review_json = json.dumps({"summary": "Checked token handling.", "findings": [
    {"severity": "major", "file": "src/auth.py", "line": 2, "title": "Crashes when TOKEN is unset", "detail": "KeyError at import."}]})

def run(who, *args, replies=None):
    env = dict(os.environ, PATH=f"{FAKE}:{os.environ['PATH']}", FAKE_GH_STATE=str(STATE), FAKE_GH_AS=who, GITHUB_REPOSITORY=REPO,
               WATCHTOWER_HOME=str(HOME), WATCHTOWER_ACP_SERVER=str(ROOT / "team/tests/fake_acp.py"), FAKE_ACP_LOG=str(LOG),
               FAKE_ACP_REPLIES=json.dumps(replies or ["{}"]))
    return subprocess.run([sys.executable, str(ROOT / "team/team.py"), *args], capture_output=True, text=True, env=env)

st = lambda: json.loads(STATE.read_text())
r = run("reviewers", "run", "--as", "barbara-gordon")
check("nothing requested -> nothing to do", r.returncode == 0 and "nothing to do" in r.stdout)
run("ci", "request", "--pr", "5")
r = run("reviewers", "run", "--as", "barbara-gordon", replies=[review_json])
print("   " + r.stdout.strip().replace("\n", "\n   "), r.stderr.strip()[-300:])
reviews = [c for c in st()["comments"]["5"] if "team:review" in c["body"]]
check("one review posted, by the reviewers' App", len(reviews) == 1 and reviews[0]["user"]["login"] == REV)
check("it carries the finding at the cited line", "Crashes when TOKEN is unset" in reviews[0]["body"] and "src/auth.py:2" in reviews[0]["body"])
prompt = [json.loads(l) for l in LOG.read_text().splitlines() if '"prompt"' in l][-1]["text"]
check("the prompt had the numbered diff", "     2 +TOKEN = os.environ['TOKEN']" in prompt)
check("the prompt hid where the change came from", "Co-Authored-By" not in prompt and "Claude" not in prompt)
conv = HOME / "google" / "google-1" / ".gemini" / "antigravity-acp" / "conversations"
check("no conversation left on disk", not any(conv.glob("*")))
check("the gate sees Barbara's review", any(row.startswith("| Barbara Gordon |") and "Changes requested" in row
                                           for row in T.gate_decide(st()["comments"]["5"], A, set())[2]))
r = run("reviewers", "run", "--as", "barbara-gordon")
check("a second check-in finds nothing new", "nothing to do" in r.stdout)

# Tim asks Barbara a question on an issue; she answers in a comment addressed to him.
run("tim", "comment", "--as", "tim-drake", "--on", "7", "--to", "Barbara Gordon", "--body", "Env vars or a secrets file?")
r = run("reviewers", "run", "--as", "barbara-gordon", replies=["Env vars, read once at start-up, never logged."])
replies = [c for c in st()["comments"]["7"] if c["user"]["login"] == REV]
check("Barbara replied to Tim, signed and addressed", len(replies) == 1 and "To: Tim Drake" in replies[0]["body"]
      and "never logged" in replies[0]["body"] and replies[0]["body"].rstrip().endswith("- Barbara Gordon"))

# Not signed in: a clear message, nothing posted.
(token_dir / "acp_token.json").unlink()
run("ci", "request", "--pr", "5")  # same sha, but a fresh round
r = run("reviewers", "run", "--as", "lucius-fox")
check("an account that is not signed in stops with the fix", r.returncode != 0 and "google-login --account google-1" in r.stderr)

print(f"\n{'all passed' if not fails else f'{fails} failed'}"); sys.exit(1 if fails else 0)
