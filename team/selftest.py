#!/usr/bin/env python3
"""Offline checks for team.py. No network. Run: python3 team/selftest.py"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import team as T  # noqa: E402

fails = 0


def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    fails += 0 if cond else 1


def C(body, at, assoc="OWNER", login="me"):
    return {"body": body, "created_at": at, "updated_at": at, "author_association": assoc,
            "user": {"login": login}, "html_url": "u"}


HEAD = "abcdef1234567890"

# Blinding
b = T.blind("## Problem\nX\n\n## Provenance\n- Tools: something\n\n## Validation\n3 passed\n"
            "Co-Authored-By: A <a@b>\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n<!-- hidden -->")
check("blind keeps substance", "X" in b and "3 passed" in b)
check("blind strips provenance, trailers, banners, hidden comments",
      all(s not in b.lower() for s in ("tools:", "co-authored", "claude", "hidden")))

# Mentions
check("To: full name", T.addressed_to("To: Barbara Gordon\nhi", "barbara-gordon"))
check("To: first names and lists", T.addressed_to("to: Lucius and Bruce", "bruce-wayne"))
check("To: group", T.addressed_to("To: reviewers", "victor-stone"))
check("group excludes non-members", not T.addressed_to("To: reviewers", "tim-drake"))
check("plain mention in prose is not addressing", not T.addressed_to("Barbara Gordon said hi", "barbara-gordon"))

# Markers
mk = T.marker("review", id="bruce-wayne", round=2, sha=HEAD[:12], data=T.enc({"v": "approve", "f": []}))
check("marker round-trips", T.parse_marker(mk + "\nbody")["round"] == "2")
check("marker never contains '--' inside", "--" not in mk[4:-3])

# Reviewer inbox
pr = {"draft": False, "head": {"sha": HEAD}}
req = C(T.marker("request", id="james-gordon", sha=HEAD[:12]), "2026-10-08T10:00:00Z", assoc="NONE", login=T.BOT)
check("reviewer owes a first review", T.reviewer_needs("barbara-gordon", pr, [req]) == "review requested")
rev = C(T.marker("review", id="barbara-gordon", round=1, sha=HEAD[:12], data=T.enc({"v": "approve", "f": []})),
        "2026-10-08T10:20:00Z")
check("reviewed current head -> nothing owed", T.reviewer_needs("barbara-gordon", pr, [req, rev]) is None)
old = dict(rev, body=rev["body"].replace(HEAD[:12], "000000000000"))
check("new commits -> owed again", T.reviewer_needs("barbara-gordon", pr, [req, old]) is not None)
req2 = dict(req, updated_at="2026-10-08T11:00:00Z")
check("fresh /review request -> owed again", T.reviewer_needs("barbara-gordon", pr, [req2, rev]) is not None)
forged = C(rev["body"], "2026-10-08T10:30:00Z", assoc="NONE", login="stranger")
check("review from an untrusted account is ignored", T.reviewer_needs("lucius-fox", pr, [req, forged]) == "review requested")
check("draft PRs are skipped", T.reviewer_needs("barbara-gordon", dict(pr, draft=True), [req]) is None)

# Implementer inbox
gate_fail = C(T.marker("gate", id="james-gordon", state="failure", sha=HEAD[:12]), "2026-10-08T11:00:00Z", "NONE", T.BOT)
check("failed gate -> Tim owes a response", T.implementer_needs("tim-drake", pr, [gate_fail]) is not None)
tim = C(T.marker("comment", id="tim-drake") + "\nfixed", "2026-10-08T11:05:00Z")
check("Tim replied after gate -> nothing owed", T.implementer_needs("tim-drake", pr, [gate_fail, tim]) is None)

# Mentions
thread = {"body": "", "created_at": "2026-10-08T09:00:00Z", "html_url": "t", "author_association": "OWNER", "user": {}}
ask = C(T.marker("comment", id="tim-drake") + "\nTo: Bruce Wayne\nIs this right?", "2026-10-08T12:00:00Z")
check("mention after my last word is pending", len(T.mention_needs("bruce-wayne", thread, [ask])) == 1)
reply = C(T.marker("comment", id="bruce-wayne") + "\nYes.", "2026-10-08T12:10:00Z")
check("mention I already answered is cleared", T.mention_needs("bruce-wayne", thread, [ask, reply]) == [])

# Cadence: hourly when quiet, every 10 minutes while there has been activity in the last hour
t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
m = lambda n: timedelta(minutes=n)
check("first ever tick checks", T.tick_due(t0, None, None))
check("quiet, 20 min since check -> sleep", not T.tick_due(t0, t0 - m(20), t0 - m(300)))
check("quiet, 60 min since check -> check", T.tick_due(t0, t0 - m(60), t0 - m(300)))
check("active, 10 min since check -> check", T.tick_due(t0, t0 - m(10), t0 - m(5)))
check("active, 5 min since check -> sleep", not T.tick_due(t0, t0 - m(5), t0 - m(2)))
check("quiet again after 60 min of no activity", not T.tick_due(t0, t0 - m(10), t0 - m(61)))

# Reviews and the gate
payload = {"summary": "ok", "verdict": "approve", "findings": [
    {"severity": "major", "file": "src/a.py", "line": 3, "title": "Crash on empty input", "detail": "d"},
    {"severity": "blocker", "file": "ghost.py", "line": 1, "title": "Phantom"}]}
r = T.build_review("bruce-wayne", payload, {"src/a.py"}, HEAD, 0, "")
check("phantom finding dropped", [f["title"] for f in r["findings"]] == ["Crash on empty input"])
check("verdict follows findings", r["verdict"] == "request_changes" and r["round"] == 1)
check("same head keeps the round", T.build_review("bruce-wayne", payload, {"src/a.py"}, HEAD, 2, HEAD[:12])["round"] == 2)
body = T.render_review(r)
check("review is signed", body.rstrip().endswith("- Bruce Wayne"))

reviews = [C(T.render_review(T.build_review(rid, {"summary": "fine"}, set(), HEAD, 0, "")), "2026-10-08T12:00:00Z")
           for rid in T.CFG["groups"]["reviewers"]]
check("all clean -> success", T.gate_decide(reviews, HEAD, set())[0] == "success")
check("one missing -> pending", T.gate_decide(reviews[1:], HEAD, set())[0] == "pending")
blocking = reviews[:2] + reviews[3:] + [C(body, "2026-10-08T12:00:00Z")]
check("blocking finding -> failure", T.gate_decide(blocking, HEAD, set())[0] == "failure")
check("override label -> success", T.gate_decide(blocking, HEAD, {"review-override"})[0] == "success")
check("reviews of an older commit do not count", T.gate_decide(reviews, "ffff" + HEAD[4:], set())[0] == "pending")

# Briefs exist and read as a colleague's brief
banned = ("artificial intelligence", "language model", " llm", " ai ", "chatbot", "assistant", "prompt engineer")
for mem in T.CFG["members"]:
    if mem["role"] == "gate":
        continue
    text = (T.BRIEFS / f"{mem['id']}.md").read_text().lower()
    check(f"brief for {mem['id']} exists and is framed as a colleague", not any(w in text for w in banned))

sys.exit(1 if fails else 0)
