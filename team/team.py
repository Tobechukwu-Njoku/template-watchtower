#!/usr/bin/env python3
"""Team tooling. Everyone uses it to find work and to post, so every comment is
signed and tracked. Talks to GitHub through the `gh` CLI: each member's own
GitHub App token locally (see `token`), the workflow token in CI. Python 3.9+,
standard library only.

  team.py brief   --as ID                        your brief
  team.py tick    --as ID [--peek]               exit 0 = time to check, 3 = not yet
  team.py inbox   --as ID [--json|--count]       what needs you now
  team.py context --as ID --pr N                 exactly what a reviewer is sent for a PR
  team.py review  --as ID --pr N --file F.json   post a review from a file
  team.py comment --as ID --on N (--body T | --body-file F) [--to "A, B"] [--needs-maintainer]
  team.py run     --as ID                        scheduler: handle ID's inbox (reviewers via Antigravity, Tim via Claude Code)
  team.py pr      --as ID --title T --body-file F  open a PR from the current branch
  team.py google-login --account A               sign a Google account in for its reviewers
  team.py token   --as ID                        scheduler: a one-hour GitHub token for ID's App
  team.py request --pr N [--force]               CI: ask reviewers for a round
  team.py gate    --pr N                         CI: decide the review/gate status
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import acp

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text())
BRIEFS = HERE / "briefs"
MEMBERS = {m["id"]: m for m in CFG["members"]}
SEVERITIES = ["blocker", "major", "minor", "nit"]
BOT = "github-actions[bot]"
MARK_RE = re.compile(r"^<!-- team:(\w+)((?: [\w-]+=[^\s]+)*) -->")
VERDICT_LABEL = {"approve": "Approved", "request_changes": "Changes requested", "comment": "Comments, not blocking"}


# --------------------------------------------------------------------------- small helpers

def now() -> datetime:
    return datetime.now(timezone.utc)


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def marker(kind: str, **kv) -> str:
    return "<!-- team:" + kind + "".join(f" {k}={v}" for k, v in kv.items()) + " -->"


def parse_marker(body: str) -> dict | None:
    m = MARK_RE.match(body or "")
    if not m:
        return None
    out = {"kind": m.group(1)}
    for pair in m.group(2).split():
        k, v = pair.split("=", 1)
        out[k] = v
    return out


def app_login(member_id: str) -> str:
    """The GitHub login a team member posts as: their App's bot, or GitHub Actions for the gate."""
    m = MEMBERS[member_id]
    return CFG["apps"][m["app"]]["slug"] + "[bot]" if m.get("app") else BOT


def login_of(c: dict) -> str | None:
    return (c.get("user") or {}).get("login")


def by(c: dict) -> str | None:
    """Team member who wrote a comment. The marker names them, and it only counts when the
    comment comes from that member's own account, so nobody can sign as someone else."""
    m = parse_marker(c.get("body", ""))
    mid = m.get("id") if m else None
    return mid if mid in MEMBERS and login_of(c) == app_login(mid) else None


def human(c: dict) -> bool:
    """A maintainer or collaborator writing as themselves (not an App, not a stranger)."""
    return ((c.get("user") or {}).get("type") != "Bot"
            and c.get("author_association") in CFG["gate"]["trusted_associations"])


def credible(c: dict) -> bool:
    """Worth acting on: a team member's own post, or a maintainer's."""
    return by(c) is not None or human(c)


def kind(c: dict) -> str | None:
    return (parse_marker(c.get("body", "")) or {}).get("kind")


def pr_from_team(pr: dict) -> bool:
    """Opened by Tim's App or by a maintainer. Anyone else waits for a maintainer's /review."""
    return login_of(pr) == app_login("tim-drake") or human(pr)


def labelled_by_human(events: list, label: str) -> bool:
    """True if the most recent time `label` was added, a person added it. Only people with
    triage access or more can label, so this rules out strangers and the team's own Apps."""
    hits = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    return bool(hits) and (hits[-1].get("actor") or {}).get("type") != "Bot"


def addressed_to(body: str, member_id: str) -> bool:
    """True if a `To:` line names this member, their first name, or one of their groups."""
    me = MEMBERS[member_id]
    names = {me["id"], me["name"].lower(), me["name"].split()[0].lower()}
    names |= {g for g, ids in CFG["groups"].items() if member_id in ids}
    for line in re.findall(r"(?im)^\s*to:\s*(.+)$", body or ""):
        for part in re.split(r",|\band\b|;", line.lower()):
            if part.strip().strip(".") in names:
                return True
    return False


def blind(text: str) -> str:
    """Strip provenance so reviewers judge the change, not who or what produced it."""
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for section in CFG["blind"]["strip_sections"]:
        text = re.sub(rf"(?ims)^#+\s*{re.escape(section)}\b.*?(?=^#+\s|\Z)", "", text)
    for pat in CFG["blind"]["strip_line_patterns"]:
        text = re.sub(pat, "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def diff_rule(path: str) -> str | None:
    """'hide' for binary files, 'trim' for lock files, vendored and generated code, else None."""
    name = os.path.basename(path)
    for rule in ("hide", "trim"):
        if any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in CFG["diff"][f"{rule}_globs"]):
            return rule
    return None


def split_files(diff: str) -> list[tuple[str, str]]:
    """A unified diff as (path, that file's part of the diff) pairs."""
    files: list[tuple[str, str]] = []
    for chunk in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(.+?) b/(.+)", chunk)
        if m:
            files.append((m.group(2), chunk))
    return files


def filter_diff(diff: str) -> tuple[str, list[str], list[str]]:
    """Drop binary files and shorten lock files, vendored and generated code, so reviewers
    still see that they changed and the start of how. Returns (diff, hidden, trimmed)."""
    kept, hidden, trimmed = [], [], []
    limit = CFG["diff"]["trim_chars"]
    for path, chunk in split_files(diff):
        rule = diff_rule(path)
        if rule == "hide":
            hidden.append(path)
            continue
        if rule == "trim" and len(chunk) > limit:
            cut = chunk[:limit].rsplit("\n", 1)[0]
            more = chunk.count("\n") - cut.count("\n")
            chunk = f"{cut}\n[... {more} more lines of this file not shown]\n"
            trimmed.append(path)
        kept.append(chunk if chunk.endswith("\n") else chunk + "\n")
    return "".join(kept), hidden, trimmed


def number_diff(diff: str) -> str:
    """Prefix each line of every hunk with its line number in the new version of the file,
    so reviewers can cite exact lines. Removed lines get no number."""
    out, line_no, in_hunk = [], 0, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            out.append(line)
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if h:
            in_hunk, line_no = True, int(h.group(1))
            out.append(line)
        elif not in_hunk or line.startswith("\\") or line.startswith("[... "):
            out.append(line)  # file headers, "\ No newline at end of file", trim notes
        elif line.startswith("-"):
            out.append(f"{'':>6} {line}")
        else:
            out.append(f"{line_no:>6} {line}")
            line_no += 1
    return "\n".join(out) + ("\n" if out else "")


def split_parts(diff: str, max_chars: int) -> list[str]:
    """Group whole files into parts of at most max_chars. A single file larger than that
    is cut at the limit."""
    parts, cur = [], ""
    for _, chunk in split_files(diff):
        if len(chunk) > max_chars:
            cut = chunk[:max_chars].rsplit("\n", 1)[0]
            chunk = cut + f"\n[... the rest of this file is over the review size limit and not shown]\n"
        if cur and len(cur) + len(chunk) > max_chars:
            parts.append(cur)
            cur = ""
        cur += chunk
    if cur:
        parts.append(cur)
    return parts


def enc(obj) -> str:
    # Standard alphabet on purpose: it has no "-", so a marker can never contain "--",
    # which would end the HTML comment early and print the data on the page.
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def dec(s: str):
    return json.loads(base64.b64decode(s.encode()))


# --------------------------------------------------------------------------- GitHub access

class GH:
    """GitHub through `gh api`. GET results are reused until the next write (or clear()),
    so one check fetches each thread once however many steps read it."""

    def __init__(self, repo: str | None = None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY") or self._detect()
        self._cache: dict = {}
        self.calls = 0

    def clear(self) -> None:
        self._cache.clear()

    @staticmethod
    def _detect() -> str:
        # From the clone's remote first: it needs no login, which matters before an App token exists.
        url = subprocess.run(["git", "-C", str(HERE), "remote", "get-url", "origin"],
                             capture_output=True, text=True).stdout.strip()
        m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
        if m:
            return m.group(1)
        out = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                             capture_output=True, text=True)
        if out.returncode != 0:
            sys.exit("could not detect the repository - run inside a clone of it")
        return out.stdout.strip()

    def api(self, path: str, method: str = "GET", body: dict | None = None, accept: str | None = None) -> str:
        key = (path, accept)
        if method == "GET" and key in self._cache:
            return self._cache[key]
        if method != "GET":
            self._cache.clear()
        self.calls += 1
        cmd = ["gh", "api", "-X", method, path if path.startswith("/") else f"/repos/{self.repo}/{path}"]
        if accept:
            cmd += ["-H", f"Accept: {accept}"]
        if body is not None:
            cmd += ["--input", "-"]
        out = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"gh api {method} {path}: {out.stderr.strip()[:300]}")
        if method == "GET":
            self._cache[key] = out.stdout
        return out.stdout

    def get(self, path: str):
        return json.loads(self.api(path) or "null")

    def paged(self, path: str) -> list:
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        while True:
            batch = self.get(f"{path}{sep}per_page=100&page={page}")
            out += batch
            if len(batch) < 100:
                return out
            page += 1

    def comments(self, n: int) -> list:
        return self.paged(f"issues/{n}/comments")

    def upsert(self, n: int, kind_: str, member_id: str, body: str) -> None:
        for c in self.comments(n):
            if kind(c) == kind_ and by(c) == member_id:
                self.api(f"issues/comments/{c['id']}", "PATCH", {"body": body})
                return
        self.api(f"issues/{n}/comments", "POST", {"body": body})

    def status(self, sha: str, state: str, desc: str) -> None:
        body = {"state": state, "context": CFG["gate"]["status_context"], "description": desc[:139]}
        if os.environ.get("GITHUB_RUN_ID"):
            body["target_url"] = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                                  f"{self.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}")
        self.api(f"statuses/{sha}", "POST", body)


# --------------------------------------------------------------------------- inbox logic (pure)

def latest(comments: list, pred) -> dict | None:
    hits = [c for c in comments if pred(c)]
    return max(hits, key=lambda c: c["updated_at"]) if hits else None


def reviewer_needs(me: str, pr: dict, comments: list) -> str | None:
    """Why a reviewer owes this PR a review, or None. Only a review request for the current
    commit counts: the team's own PRs get one on every push, while a PR from outside the
    team waits for a maintainer's /review each time its author pushes."""
    if pr.get("draft"):
        return None
    head = pr["head"]["sha"][:12]
    mine = latest(comments, lambda c: kind(c) == "review" and by(c) == me)
    req = latest(comments, lambda c: kind(c) == "request" and by(c) == "james-gordon")
    if not req or parse_marker(req["body"]).get("sha") != head:
        return None
    if not mine:
        return "review requested"
    if parse_marker(mine["body"]).get("sha") != head:
        return "new commits since your last review"
    if req and req["updated_at"] > mine["updated_at"]:
        return "fresh round requested"
    return None


def implementer_needs(me: str, pr: dict, comments: list) -> str | None:
    """Tim owes a response when the gate failed on the current head of his own PR and he has
    not replied since. Other people's PRs are never his: he would be running their code."""
    if login_of(pr) != app_login(me):
        return None
    gate = latest(comments, lambda c: kind(c) == "gate" and by(c) == "james-gordon")
    if not gate:
        return None
    m = parse_marker(gate["body"])
    if m.get("state") != "failure" or m.get("sha") != pr["head"]["sha"][:12]:
        return None
    mine = latest(comments, lambda c: by(c) == me)
    if mine and mine["updated_at"] >= gate["updated_at"]:
        return None
    return "review findings to address"


def mention_needs(me: str, thread: dict, comments: list) -> list[dict]:
    """Comments (and the opening post) addressed to me that came after my last word on the thread."""
    mine = latest(comments, lambda c: by(c) == me)
    since = mine["updated_at"] if mine else ""  # reviews are edited in place, so use the edit time
    opening = {"body": thread.get("body") or "", "created_at": thread["created_at"],
               "updated_at": thread["created_at"], "author_association": thread.get("author_association"),
               "user": thread.get("user"), "html_url": thread["html_url"]}
    out = []
    for c in [opening] + comments:
        if by(c) == me or not credible(c) or c["created_at"] <= since:
            continue
        if addressed_to(c["body"], me):
            out.append(c)
    return out


def tick_due(now_: datetime, last_check: datetime | None, last_activity: datetime | None) -> bool:
    cad = CFG["cadence"]
    slack = timedelta(minutes=1)  # schedulers drift; do not skip a slot by seconds
    active = last_activity is not None and now_ - last_activity < timedelta(minutes=cad["active_window_minutes"])
    every = cad["active_check_minutes"] if active else cad["quiet_check_minutes"]
    return last_check is None or now_ - last_check >= timedelta(minutes=every) - slack


# --------------------------------------------------------------------------- review building (pure)

def build_review(me: str, payload: dict, pr_files: set, head: str, prev_round: int, prev_sha: str) -> dict:
    findings, dropped = [], []
    for f in payload.get("findings") or []:
        f = dict(f)
        f["severity"] = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
        (findings if f.get("file") in pr_files else dropped).append(f)
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    # The verdict follows the findings, so a review cannot say "approve" while listing a blocker.
    if any(f["severity"] in CFG["gate"]["blocking_severities"] for f in findings):
        verdict = "request_changes"
    elif any(f["severity"] == "minor" for f in findings):
        verdict = "comment"
    else:
        verdict = "approve"
    rnd = prev_round if prev_sha == head[:12] and prev_round else prev_round + 1
    return {"id": me, "verdict": verdict, "summary": str(payload.get("summary", "")).strip(),
            "findings": findings, "dropped": dropped, "resolved": [str(r) for r in payload.get("resolved") or []],
            "round": max(rnd, 1), "sha": head[:12]}


def render_review(r: dict) -> str:
    m = MEMBERS[r["id"]]
    compact = {"v": r["verdict"], "f": [[f["severity"], f.get("file"), f.get("line"), f.get("title", "")]
                                       for f in r["findings"]]}
    out = [marker("review", id=r["id"], round=r["round"], sha=r["sha"], data=enc(compact)),
           f"### {m['focus']}",
           f"**{VERDICT_LABEL[r['verdict']]}** · round {r['round']} · `{r['sha'][:7]}`", "", r["summary"]]
    if r["findings"]:
        out += ["", "| Severity | Finding | Where |", "|---|---|---|"]
        out += [f"| {f['severity']} | {str(f.get('title', '')).replace('|', '/')} | `{f.get('file')}:{f.get('line', '')}` |"
                for f in r["findings"]]
        out.append("")
        for i, f in enumerate(r["findings"], 1):
            out += [f"**{i}. [{f['severity']}] {f.get('title', '')}** - `{f.get('file')}:{f.get('line', '')}`", "",
                    str(f.get("detail", "")).strip()]
            if f.get("suggestion"):
                out += ["", f"Suggestion: {str(f['suggestion']).strip()}"]
            out.append("")
    if r["resolved"]:
        out += ["", "Resolved since last round:"] + [f"- {x}" for x in r["resolved"]]
    out += ["", f"- {m['name']}"]
    return "\n".join(out)


def gate_decide(comments: list, head: str, labels: set) -> tuple[str, str, list[str], list[str]]:
    g = CFG["gate"]
    rows, failing, missing, rounds = [], [], [], []
    for rid in CFG["groups"]["reviewers"]:
        m = MEMBERS[rid]
        c = latest(comments, lambda c: kind(c) == "review" and by(c) == rid)
        mk = parse_marker(c["body"]) if c else None
        if not mk or mk.get("sha") != head[:12]:
            rows.append(f"| {m['name']} | {m['focus']} | waiting | - |")
            missing.append(m["name"])
            continue
        data = dec(mk["data"])
        rounds.append(int(mk.get("round", 1)))
        counts = {s: sum(1 for f in data["f"] if f[0] == s) for s in SEVERITIES}
        tally = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
        rows.append(f"| {m['name']} | {m['focus']} | {VERDICT_LABEL[data['v']]} | {tally} |")
        if any(f[0] in g["blocking_severities"] for f in data["f"]):
            failing.append(f"{m['name']}: blocking findings")

    if CFG["labels"]["override"] in labels:
        return "success", f"Overridden by maintainer label `{CFG['labels']['override']}`.", rows, []
    if failing:
        capped = rounds and max(rounds) >= g["max_rounds"]
        head_line = ("Round cap reached with blocking findings - needs a maintainer decision."
                     if capped else "Not ready to merge: blocking findings to address or rebut.")
        return "failure", head_line, rows, failing
    if missing:
        return "pending", f"Waiting for {', '.join(missing)}.", rows, []
    return "success", "Cleared to merge once CI is green.", rows, []


# --------------------------------------------------------------------------- reviewer prompts (pure)

def author_label(c: dict) -> str:
    who = by(c)
    return MEMBERS[who]["name"] if who else ("The maintainer" if human(c) else "Someone outside the team")


def reply_to(c: dict) -> str:
    who = by(c)
    return MEMBERS[who]["name"] if who else "maintainer"


def handbook_section(title: str) -> str:
    """One section of AGENTS.md, without its placeholder comments."""
    text = (HERE.parent / "AGENTS.md").read_text()
    m = re.search(rf"(?ms)^## {re.escape(title)}[ \t]*$(.*?)(?=^## |\Z)", text)
    return re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).strip() if m else ""


def pr_header(pr: dict, commits: list[str], prev: dict | None, replies: list[dict]) -> str:
    s = [f"# Pull request #{pr.get('number', '')}: {blind(pr['title'])}", "", "## Description", "",
         blind(pr.get("body") or "") or "(none)"]
    if commits:
        s += ["", "## Commit messages", "", "\n---\n".join(blind(c) for c in commits)]
    if prev:
        s += ["", "## Your previous review", "", blind(prev["body"])]
        if replies:
            s += ["", "## Replies since", ""] + [f"{author_label(c)}:\n{blind(c['body'])}\n---" for c in replies]
    return "\n".join(s)


def team_context() -> list[str]:
    s = []
    project = handbook_section("The project")
    if project:
        s.append("About the project:\n\n" + project)
    rules = handbook_section("Rules")
    if rules:
        s.append("The team's rules, which every pull request must follow:\n\n" + rules)
    return s


def reviewer_prompt(me: str, header: str, part: str, index: int, total: int, hidden: list, trimmed: list) -> str:
    """Everything a reviewer is sent: their brief, the team's rules, the format, the PR."""
    principles, fmt = (BRIEFS / "_reviewer.md").read_text().split("Review format", 1)
    s = [principles.strip(), (BRIEFS / f"{me}.md").read_text().strip(), *team_context(),
         "Review format" + fmt.rstrip(),
         "Everything you need is below; you cannot run commands or open other files. "
         "Reply with the JSON object only, no other text.",
         "=" * 20 + " PULL REQUEST " + "=" * 20, header, "## Diff"]
    if total > 1:
        s.append(f"This change is large, so it is reviewed in {total} parts. This is part {index} of {total}: "
                 "review the files in this part; the others are covered separately.")
    if hidden:
        s.append("Binary files changed, not shown: " + ", ".join(hidden))
    if trimmed:
        s.append("Shown only in part because of their size: " + ", ".join(trimmed))
    s.append("```diff\n" + part.rstrip() + "\n```")
    return "\n\n".join(s) + "\n"


def reply_prompt(me: str, thread: dict, comments: list, ask: dict) -> str:
    """For a question addressed to a reviewer outside a review."""
    convo = [f"# {blind(thread.get('title', ''))}", "", f"{author_label(thread)} opened it:",
             blind(thread.get("body") or "") or "(no description)"]
    for c in [c for c in comments if credible(c) and kind(c) not in ("request", "gate")][-20:]:
        convo += ["", f"{author_label(c)}:", blind(c["body"])]
    s = [(BRIEFS / f"{me}.md").read_text().strip(), *team_context(),
         f"{author_label(ask)} has addressed you in the conversation below. Reply in a few plain sentences, "
         "from your lens: answer the question, or say what you need to answer it. No JSON, no headings and no "
         "sign-off; your name is added for you. Everything you need is below; you cannot run commands or open "
         "other files.",
         "=" * 20 + " CONVERSATION " + "=" * 20, "\n".join(convo),
         "=" * 20 + " THE MESSAGE TO ANSWER " + "=" * 20, f"{author_label(ask)}:\n{blind(ask['body'])}"]
    return "\n\n".join(s) + "\n"


def parse_review_reply(text: str) -> dict:
    """The review JSON in a model's reply, allowing for a code fence or stray words around it."""
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < i:
        raise ValueError("no JSON object in the reply")
    payload = json.loads(text[i:j + 1])
    findings = payload.get("findings", []) if isinstance(payload, dict) else None
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        raise ValueError("the reply is not in the review format")
    return payload


def merge_payloads(payloads: list[dict]) -> dict:
    """One review from the reviews of each part of a large PR."""
    if len(payloads) == 1:
        return payloads[0]
    return {"summary": " ".join(f"Part {i}: {str(p.get('summary', '')).strip()}" for i, p in enumerate(payloads, 1)),
            "findings": [f for p in payloads for f in p.get("findings") or []],
            "resolved": sorted({str(r) for p in payloads for r in p.get("resolved") or []})}


def too_large_payload(first_file: str, chars: int, parts: int) -> dict:
    d = CFG["diff"]
    return {"summary": "This change is too large to review well in one go, so I have not reviewed it.",
            "findings": [{"severity": "major", "file": first_file, "line": 1, "title": "Change too large to review",
                          "detail": f"The diff is about {chars:,} characters, which would take {parts} review parts; "
                                    f"the limit is {d['max_parts']} parts of {d['max_part_chars']:,} characters.",
                          "suggestion": "Split it into smaller pull requests, one concern each."}]}


# --------------------------------------------------------------------------- commands

def cmd_brief(a) -> int:
    m = MEMBERS[a.as_]
    if m["role"] == "reviewer":
        print((BRIEFS / "_reviewer.md").read_text())
    print((BRIEFS / f"{m['id']}.md").read_text())
    return 0


def wt_home() -> Path:
    return Path(os.environ.get("WATCHTOWER_HOME", Path.home() / ".watchtower"))


def state_path(repo: str, me: str) -> Path:
    p = wt_home() / repo.replace("/", "__") / f"{me}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# --------------------------------------------------------------------------- App tokens

def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def app_jwt(issuer, pem: Path, now_: int | None = None) -> str:
    """A ten-minute JSON Web Token that proves we hold the App's private key.
    Signed with the openssl command, since the standard library has no RSA."""
    t = int(time.time()) if now_ is None else now_
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"iat": t - 60, "exp": t + 540, "iss": issuer}, separators=(",", ":")).encode())
    signed = f"{head}.{body}"
    out = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(pem)], input=signed.encode(), capture_output=True)
    if out.returncode != 0:
        raise RuntimeError(f"openssl could not sign with {pem}: {out.stderr.decode().strip()[:200]}")
    return f"{signed}.{b64url(out.stdout)}"


def github_as_app(method: str, path: str, jwt: str, body: dict | None = None) -> dict:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    req = urllib.request.Request(f"{api}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {jwt}",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "watchtower-team"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read().decode()[:200]}") from None


def cmd_token(a) -> int:
    """Print a one-hour installation token for a member's App, limited to this repository.
    The scheduler puts it in GH_TOKEN, so `gh` and git act as that member."""
    if "WATCHTOWER_AGENT" in os.environ:  # set, even to nothing, means an agent is asking
        sys.exit("token is for the scheduler, not for a running agent")
    m = MEMBERS[a.as_]
    if not m.get("app"):
        sys.exit(f"{a.as_} posts as GitHub Actions and has no App")
    slug = CFG["apps"][m["app"]]["slug"]
    keys = wt_home() / "apps"
    try:
        meta = json.loads((keys / f"{slug}.json").read_text())
    except FileNotFoundError:
        sys.exit(f"no credentials for {slug} in {keys} - run scripts/create-apps.py")
    jwt = app_jwt(meta.get("client_id") or int(meta["id"]), keys / f"{slug}.pem")
    repo = GH._detect() if not os.environ.get("GITHUB_REPOSITORY") else os.environ["GITHUB_REPOSITORY"]
    try:
        inst = github_as_app("GET", f"/repos/{repo}/installation", jwt)
    except RuntimeError as e:
        sys.exit(f"{slug} is not installed on {repo} - install it at "
                 f"https://github.com/apps/{slug}/installations/new ({e})")
    tok = github_as_app("POST", f"/app/installations/{inst['id']}/access_tokens", jwt,
                        {"repositories": [repo.split("/", 1)[1]]})
    print(tok["token"])
    return 0


def cmd_tick(a) -> int:
    gh = GH()
    sp = state_path(gh.repo, a.as_)
    st = json.loads(sp.read_text()) if sp.exists() else {}
    newest = gh.get("issues?state=all&sort=updated&direction=desc&per_page=1")
    repo = gh.get(f"/repos/{gh.repo}")
    stamps = [ts(x) for x in [repo.get("pushed_at")] + [i["updated_at"] for i in newest] if x]
    last_activity = max(stamps) if stamps else None
    last_check = ts(st["last_check"]) if st.get("last_check") else None
    due = tick_due(now(), last_check, last_activity)
    mode = "active" if last_activity and now() - last_activity < timedelta(minutes=CFG["cadence"]["active_window_minutes"]) else "quiet"
    print(f"{'check' if due else 'sleep'} ({mode})")
    if due and not a.peek:
        sp.write_text(json.dumps({"last_check": now().strftime("%Y-%m-%dT%H:%M:%SZ")}))
    return 0 if due else 3


def gather_inbox(gh: GH, me: str) -> list[dict]:
    role = MEMBERS[me]["role"]
    items, seen = [], set()
    since = (now() - timedelta(days=CFG["cadence"]["mention_lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    threads = gh.paged(f"issues?state=open&since={since}")
    for t in threads:
        n, is_pr = t["number"], "pull_request" in t
        comments = gh.comments(n) if (t["comments"] or is_pr) else []
        if is_pr and role in ("reviewer", "implementer"):
            pr = gh.get(f"pulls/{n}")
            why = reviewer_needs(me, pr, comments) if role == "reviewer" else implementer_needs(me, pr, comments)
            if why:
                verb = "Review" if role == "reviewer" else "Address review on"
                items.append({"number": n, "kind": "pr", "is_pr": True, "title": t["title"], "why": why, "url": t["html_url"],
                              "next": (f"python3 team/team.py context --as {me} --pr {n}" if role == "reviewer"
                                       else f"gh pr view {n} --comments"), "action": f"{verb} PR #{n}"})
                seen.add(n)
        labels = {l["name"] for l in t.get("labels", [])}
        if (role == "implementer" and not is_pr and CFG["labels"]["ready"] in labels
                and not any(by(c) == me for c in comments)
                and labelled_by_human(gh.paged(f"issues/{n}/events"), CFG["labels"]["ready"])):
            items.append({"number": n, "kind": "issue", "is_pr": False, "title": t["title"], "why": "ready for implementation",
                          "url": t["html_url"], "action": f"Pick up issue #{n}", "next": f"gh issue view {n} --comments"})
            seen.add(n)
        for c in mention_needs(me, t, comments):
            if n in seen:
                break
            items.append({"number": n, "kind": "mention", "is_pr": is_pr, "title": t["title"], "why": "addressed to you",
                          "url": c["html_url"], "action": f"Reply on #{n}",
                          "next": f"gh {'pr' if is_pr else 'issue'} view {n} --comments"})
            seen.add(n)
    return items


def cmd_inbox(a) -> int:
    items = gather_inbox(GH(), a.as_)
    if a.count:
        print(len(items))
    elif a.json:
        print(json.dumps(items, indent=2))
    elif not items:
        print("Nothing needs you right now.")
    else:
        for i in items:
            print(f"- {i['action']}: {i['title']} ({i['why']})\n    {i['url']}\n    next: {i['next']}")
    return 0


def pr_text(gh: GH, n: int, me: str) -> tuple[dict, str, str, list[str], list[str]]:
    """A PR as a reviewer sees it: (pr, header text, numbered diff, hidden files, trimmed files)."""
    pr = gh.get(f"pulls/{n}")
    comments = gh.comments(n)
    diff, hidden, trimmed = filter_diff(gh.api(f"pulls/{n}", accept="application/vnd.github.v3.diff"))
    commits = [c["commit"]["message"] for c in gh.paged(f"pulls/{n}/commits")][-20:]
    prev = latest(comments, lambda c: kind(c) == "review" and by(c) == me)
    replies = [c for c in comments if prev and c["created_at"] > prev["updated_at"] and credible(c) and by(c) != me
               and (by(c) == "tim-drake" or addressed_to(c["body"], me))]
    return pr, pr_header(pr, commits, prev, replies), number_diff(diff), hidden, trimmed


def review_pr(gh: GH, ask, me: str, n: int, post: bool = True) -> dict:
    """Review PR #n as `me`. `ask(prompt)` returns a review payload; one call per part."""
    pr, header, diff, hidden, trimmed = pr_text(gh, n, me)
    d = CFG["diff"]
    parts = split_parts(diff, d["max_part_chars"]) or [""]
    if len(parts) > d["max_parts"]:
        payload = too_large_payload(split_files(diff)[0][0], len(diff), len(parts))
    else:
        payload = merge_payloads([ask(reviewer_prompt(me, header, p, i, len(parts), hidden, trimmed))
                                  for i, p in enumerate(parts, 1)])
    return post_review(gh, me, n, payload) if post else payload


def post_review(gh: GH, me: str, n: int, payload: dict) -> dict:
    pr = gh.get(f"pulls/{n}")
    files = {f["filename"] for f in gh.paged(f"pulls/{n}/files")}
    prev = latest(gh.comments(n), lambda c: kind(c) == "review" and by(c) == me)
    pm = parse_marker(prev["body"]) if prev else {}
    r = build_review(me, payload, files, pr["head"]["sha"], int(pm.get("round", 0)), pm.get("sha", ""))
    gh.upsert(n, "review", me, render_review(r))
    return r


def post_comment(gh: GH, member_id: str, n: int, body: str, to: str | None = None, needs_maintainer: bool = False) -> None:
    m = MEMBERS[member_id]
    head = [f"To: {to}"] if to else (["To: maintainer"] if needs_maintainer else [])
    sign = f"- {m['name']}"
    text = "\n".join([marker("comment", id=m["id"])] + head + ([""] if head else []) + [body.rstrip()]
                     + ([] if body.rstrip().endswith(sign) else ["", sign]))
    gh.api(f"issues/{n}/comments", "POST", {"body": text})
    if needs_maintainer:
        gh.api(f"issues/{n}/labels", "POST", {"labels": [CFG["labels"]["needs_maintainer"]]})


def cmd_context(a) -> int:
    """Print exactly what a reviewer would be sent for PR #N (one prompt per part)."""
    prompts: list[str] = []
    review_pr(GH(), lambda p: prompts.append(p) or {"summary": "", "findings": []}, a.as_, a.pr, post=False)
    print(("\n\n" + "#" * 30 + " next part " + "#" * 30 + "\n\n").join(prompts) or "(too large: no prompt is sent)")
    return 0


def cmd_review(a) -> int:
    """Post a review from a JSON file (for testing, or to post a review by hand)."""
    if MEMBERS[a.as_]["role"] != "reviewer":
        sys.exit(f"{a.as_} is not a reviewer")
    r = post_review(GH(), a.as_, a.pr, json.loads(Path(a.file).read_text()))
    print(f"posted: {VERDICT_LABEL[r['verdict']]}, round {r['round']}, {len(r['findings'])} finding(s)"
          + (f"; dropped {len(r['dropped'])} citing files outside the change" if r["dropped"] else ""))
    return 0


def cmd_comment(a) -> int:
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    if not body.strip():
        sys.exit("empty comment")
    post_comment(GH(), a.as_, a.on, body, a.to, a.needs_maintainer)
    print("posted")
    return 0


# --------------------------------------------------------------------------- reviewers, through Antigravity

def google_home(account: str) -> Path:
    return wt_home() / "google" / account


@contextlib.contextmanager
def account_lock(account: str):
    """One connector at a time per Google account, so two reviewers never refresh the same
    login at once."""
    path = wt_home() / "google" / f"{account}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def connector() -> dict:
    """The connector chosen at the last google-login. Pinned on purpose: if T3 Code replaces
    it, reviews stop with a clear message instead of silently running on a new version."""
    if os.environ.get("WATCHTOWER_ACP_SERVER"):
        return {"path": os.environ["WATCHTOWER_ACP_SERVER"], "version": "set by WATCHTOWER_ACP_SERVER"}
    try:
        info = json.loads((wt_home() / "connector.json").read_text())
    except FileNotFoundError:
        sys.exit("no connector chosen yet - run: python3 team/team.py google-login --account google-1")
    if not Path(info["path"]).exists():
        now_ = acp.find_installed_connector()
        sys.exit(f"the Antigravity connector {info['version']} is gone (T3 Code has probably replaced it"
                 + (f" with {now_['version']}" if now_ else "") + "). Run team.py google-login again to switch to it, "
                 "then read the next reviews closely.")
    return info


def asker(conn, model: str, timeout: float):
    """`ask(prompt)` for review_pr: a fresh session per prompt, and one retry if the reply
    is not valid review JSON."""
    def ask(prompt: str) -> dict:
        sid, _ = conn.new_session()
        conn.set_model(sid, model)
        reply = conn.prompt(sid, prompt, timeout)
        try:
            return parse_review_reply(reply)
        except ValueError:
            reply = conn.prompt(sid, "That reply was not a single JSON object in the review format. "
                                     "Send the review again as the JSON object only.", timeout)
            return parse_review_reply(reply)
    return ask


def cmd_run(a) -> int:
    """A member's check-in: handle everything in their inbox, then stop."""
    me = a.as_
    if MEMBERS[me]["role"] == "implementer":
        return run_implementer(me)
    if MEMBERS[me]["role"] != "reviewer":
        sys.exit(f"{me} has no check-in: the gate runs in GitHub Actions")
    gh = GH()
    items = gather_inbox(gh, me)
    if not items:
        print("nothing to do")
        return 0
    rc = CFG["reviewing"]["members"][me]
    info, timeout = connector(), CFG["reviewing"]["timeout_minutes"] * 60
    failed = 0
    with account_lock(rc["account"]), acp.Connector(info["path"], google_home(rc["account"])) as conn:
        ask = asker(conn, rc["model"], timeout)
        for it in items:
            n = it["number"]
            gh.clear()  # fresh data for each item, reused within it
            try:
                comments = gh.comments(n)
                reviewed = any(kind(c) == "review" and by(c) == me for c in comments)
                if it["kind"] == "pr" or (it["is_pr"] and reviewed):
                    r = review_pr(gh, ask, me, n)
                    print(f"#{n}: {VERDICT_LABEL[r['verdict']]}, {len(r['findings'])} finding(s), round {r['round']}")
                else:
                    thread = gh.get(f"issues/{n}")
                    asks = mention_needs(me, thread, comments)
                    if not asks:
                        continue
                    sid, _ = conn.new_session()
                    conn.set_model(sid, rc["model"])
                    text = conn.prompt(sid, reply_prompt(me, thread, comments, asks[-1]), timeout).strip()
                    if not text:
                        raise acp.AcpError("empty reply")
                    post_comment(gh, me, n, text, to=reply_to(asks[-1]))
                    print(f"#{n}: replied to {reply_to(asks[-1])}")
            except acp.NeedsLogin:
                sys.exit(f"{rc['account']} is not signed in - run: python3 team/team.py google-login --account {rc['account']}")
            except (acp.AcpError, ValueError, RuntimeError) as e:
                failed += 1
                print(f"#{n}: skipped - {e}", file=sys.stderr)
        if conn.denied:
            print(f"refused tool requests: {', '.join(conn.denied)}", file=sys.stderr)
    print(f"connector {info['version']}, account {rc['account']}, model {rc['model']}")
    return 1 if failed else 0


def google_email(home: Path) -> str | None:
    """Which Google account a home folder is signed in to, asked of Google itself."""
    try:
        d = json.loads((home / ".gemini" / "antigravity-acp" / "acp_token.json").read_text())
        body = urllib.parse.urlencode({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                       "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
        with urllib.request.urlopen(urllib.request.Request(d["token_uri"], data=body), timeout=20) as r:
            tok = json.load(r)["access_token"]
        req = urllib.request.Request("https://openidconnect.googleapis.com/v1/userinfo",
                                     headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r).get("email")
    except (OSError, ValueError, KeyError):
        return None


def cmd_google_login(a) -> int:
    """Sign a Google account in for the reviewers who use it (once per account), or show
    which account it is signed in to. Also pins the connector version."""
    found = ({"path": os.environ["WATCHTOWER_ACP_SERVER"], "version": "set by WATCHTOWER_ACP_SERVER"}
             if os.environ.get("WATCHTOWER_ACP_SERVER") else acp.find_installed_connector())
    if not found:
        sys.exit("Antigravity connector not found. Install T3 Code, use its Antigravity provider once "
                 "(that downloads the connector), then run this again.")
    users = {m: r for m, r in CFG["reviewing"]["members"].items() if r["account"] == a.account}
    if not users:
        sys.exit(f"no reviewer uses {a.account}; accounts in team/config.json: "
                 + ", ".join(sorted({r['account'] for r in CFG['reviewing']['members'].values()})))
    names = ", ".join(MEMBERS[m]["name"] for m in users)
    home = google_home(a.account)

    def show_link(line: str) -> None:
        m = re.search(r"https://accounts\.google\.com/\S+", line)
        if m:
            print(f"If no browser window opened, open this link:\n{m.group(0)}\n", file=sys.stderr)

    with account_lock(a.account), acp.Connector(found["path"], home, on_stderr=show_link) as conn:
        try:
            _, options = conn.new_session()
        except acp.NeedsLogin:
            print(f"Sign in with the Google account for {names}. A browser window will open.")
            conn.login()
            _, options = conn.new_session()
    (wt_home() / "connector.json").write_text(json.dumps(found, indent=2) + "\n")
    print(f"{a.account}: signed in as {google_email(home) or '(could not ask Google which account)'}; used by {names}")
    print(f"connector pinned: {found['version']}")
    missing = sorted({r["model"] for r in users.values()} - set(acp.model_choices(options)))
    if missing:
        print(f"! this account does not offer {', '.join(missing)} - choose from: "
              + ", ".join(acp.model_choices(options)), file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------- Tim, through Claude Code

def clone_path(repo: str, member_id: str) -> Path:
    return wt_home() / "clones" / repo.replace("/", "__") / member_id


def git(path: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {out.stderr.strip()[:300]}")
    return out.stdout.strip()


# Git asks this for credentials, and it answers with the member's App token from the
# environment. Nothing is stored, and the token lasts an hour.
CREDENTIAL_HELPER = '!f() { test "$1" = get && echo username=x-access-token && echo "password=$GH_TOKEN"; }; f'


def bot_user_id(gh: GH, slug: str) -> int:
    try:
        meta = json.loads((wt_home() / "apps" / f"{slug}.json").read_text())
        if meta.get("bot_user_id"):
            return int(meta["bot_user_id"])
    except (OSError, ValueError):
        pass
    return int(gh.get(f"/users/{urllib.parse.quote(slug + '[bot]')}")["id"])


def ensure_clone(gh: GH, member_id: str) -> Path:
    """The member's own clone, under ~/.watchtower so background jobs may use it, set up to
    commit and push as the member's App and nobody else."""
    path = clone_path(gh.repo, member_id)
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = os.environ.get("WATCHTOWER_CLONE_URL") or f"https://github.com/{gh.repo}.git"
        out = subprocess.run(["git", "clone", "-q", url, str(path)], capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"could not clone {url}: {out.stderr.strip()[:300]}")
    slug = CFG["apps"][MEMBERS[member_id]["app"]]["slug"]
    git(path, "config", "user.name", f"{slug}[bot]")
    git(path, "config", "user.email", f"{bot_user_id(gh, slug)}+{slug}[bot]@users.noreply.github.com")
    # An empty helper first clears any your system or user git config adds (such as the
    # macOS keychain), so a push can never fall back to your own GitHub login.
    git(path, "config", "--replace-all", "credential.helper", "")
    git(path, "config", "--add", "credential.helper", CREDENTIAL_HELPER)
    git(path, "config", "core.hooksPath", ".githooks")
    git(path, "fetch", "-q", "--prune", "origin")
    if git(path, "branch", "--show-current") == "main" and not git(path, "status", "--porcelain"):
        git(path, "merge", "-q", "--ff-only", "origin/main")
    return path


def claude_settings(clone: Path) -> dict:
    """Tim's permissions. With --permission-mode dontAsk anything not allowed here is refused;
    reading files inside the clone needs no rule, and edits are allowed there only."""
    cfg = CFG["implementing"]
    return {"permissions": {"allow": cfg["allow"] + cfg["extra_allow"] + [f"Edit(/{clone.resolve()}/**)"],
                            "deny": cfg["deny"]},
            "attribution": {"commit": "", "pr": "", "sessionUrl": False},
            "autoMemoryEnabled": False}


def claude_command(clone: Path, prompt: str) -> list[str]:
    return ["claude", "-p", prompt, "--model", CFG["implementing"]["model"],
            "--permission-mode", "dontAsk",         # refuse whatever is not allowed, never ask
            "--setting-sources", "project",         # the repo's settings, not your personal ones
            "--settings", json.dumps(claude_settings(clone)),
            "--no-session-persistence"]             # keep no transcript on disk


def claude_env(me: str) -> dict:
    """Tim's environment: the scheduler's, minus Claude settings inherited from any Claude
    session you started this from (they can switch off the normal login), plus the
    long-lived login from `claude setup-token` if you saved one to ~/.watchtower/claude-token."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC"))}
    env["WATCHTOWER_AGENT"] = me  # GH_TOKEN, his own App's, is kept
    token = wt_home() / "claude-token"
    if token.exists():
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.read_text().strip()
    return env


def run_implementer(me: str) -> int:
    gh = GH()
    items = gather_inbox(gh, me)
    if not items:
        print("nothing to do")
        return 0
    clone = ensure_clone(gh, me)
    name = MEMBERS[me]["name"]
    prompt = (f"You are {name}. It is time for your scheduled check on this repository. Read AGENTS.md and your "
              f"brief (python3 team/team.py brief --as {me}), then run python3 team/team.py inbox --as {me} and "
              "handle every item in it. Post only through team/team.py, and open pull requests with "
              f"python3 team/team.py pr --as {me}. Stop when your inbox is empty.")
    env = claude_env(me)
    print(f"{len(items)} item(s); starting Claude Code ({CFG['implementing']['model']}) in {clone}", flush=True)
    try:
        r = subprocess.run(claude_command(clone, prompt), cwd=clone, env=env,
                           timeout=CFG["implementing"]["timeout_minutes"] * 60)
    except subprocess.TimeoutExpired:
        print("stopped: took longer than implementing.timeout_minutes", file=sys.stderr)
        return 1
    return r.returncode


def cmd_pr(a) -> int:
    """Open a pull request from the current branch, signed, as the member's App."""
    m = MEMBERS[a.as_]
    gh = GH()
    here = Path.cwd()
    branch = git(here, "branch", "--show-current")
    base = gh.get(f"/repos/{gh.repo}")["default_branch"]
    if not branch or branch == base:
        sys.exit(f"switch to your feature branch first (you are on {branch or 'a detached HEAD'})")
    if not git(here, "ls-remote", "--heads", "origin", branch, check=False):
        sys.exit(f"push the branch first: git push -u origin {branch}")
    body = Path(a.body_file).read_text().rstrip()
    sign = f"- {m['name']}"
    if not body.endswith(sign):
        body += f"\n\n{sign}"
    pr = json.loads(gh.api("pulls", "POST", {"title": a.title, "head": branch, "base": base, "body": body + "\n"}))
    print(pr["html_url"])
    return 0


def cmd_request(a) -> int:
    gh = GH()
    pr = gh.get(f"pulls/{a.pr}")
    if pr.get("draft"):
        print("draft - no review requested")
        return 0
    sha = pr["head"]["sha"]
    if not a.force and not pr_from_team(pr):
        # Reviewers read whatever is in a PR, so strangers do not get their attention by
        # opening one. A maintainer's "/review" comment runs this again with --force.
        try:  # Dependabot PRs get a read-only token here; nothing to do for them either way
            gh.status(sha, "pending", "Opened outside the team - a maintainer comments /review to start")
        except RuntimeError:
            pass
        print(f"opened by {login_of(pr)} - waiting for a maintainer's /review")
        return 0
    names = ", ".join(MEMBERS[r]["name"] for r in CFG["groups"]["reviewers"])
    body = "\n".join([marker("request", id="james-gordon", sha=sha[:12]), f"To: {names}", "",
                      f"Commit `{sha[:7]}` is ready for review. Each of you: one review from your lens, "
                      "posted with `team/team.py review`.", "", "- James Gordon"])
    gh.upsert(a.pr, "request", "james-gordon", body)
    gh.status(sha, "pending", "Waiting for the review team")
    print("requested")
    return 0


def cmd_gate(a) -> int:
    gh = GH()
    pr = gh.get(f"pulls/{a.pr}")
    sha = pr["head"]["sha"]
    labels = {l["name"] for l in pr.get("labels", [])}
    comments = gh.comments(a.pr)
    state, headline, rows, failing = gate_decide(comments, sha, labels)

    trailers = set()
    for c in gh.paged(f"pulls/{a.pr}/commits"):
        trailers |= set(re.findall(r"(?im)^co-authored-by:\s*(.+)$", c["commit"]["message"]))

    body = [marker("gate", id="james-gordon", state=state, sha=sha[:12])]
    if state == "failure":
        body += ["To: Tim Drake", ""]
    body += ["### Review gate", f"**{headline}**", "", "| Reviewer | Lens | Verdict | Findings |", "|---|---|---|---|", *rows]
    if failing:
        body += ["", "Blocking:"] + [f"- {x}" for x in failing]
    if trailers:  # for the maintainer; reviewers read PRs through `context`, which strips this
        body += ["", "<details><summary>Provenance</summary>", ""] + [f"- Co-authored-by: {t.strip()}" for t in sorted(trailers)] + ["", "</details>"]
    body += ["", "- James Gordon"]
    gh.upsert(a.pr, "gate", "james-gordon", "\n".join(body))
    gh.status(sha, state, headline)
    print(f"{state}: {headline}")
    return 0 if state != "failure" else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ids = [m["id"] for m in CFG["members"]]

    def p(name, who=True):
        sp = sub.add_parser(name)
        if who:
            sp.add_argument("--as", dest="as_", required=True, choices=ids)
        return sp

    p("brief")
    t = p("tick"); t.add_argument("--peek", action="store_true")
    i = p("inbox"); i.add_argument("--json", action="store_true"); i.add_argument("--count", action="store_true")
    c = p("context"); c.add_argument("--pr", type=int, required=True)
    r = p("review"); r.add_argument("--pr", type=int, required=True); r.add_argument("--file", required=True)
    m = p("comment"); m.add_argument("--on", type=int, required=True); m.add_argument("--body"); m.add_argument("--body-file")
    m.add_argument("--to"); m.add_argument("--needs-maintainer", action="store_true")
    p("token")
    p("run")
    lg = p("google-login", who=False); lg.add_argument("--account", required=True)
    pr = p("pr"); pr.add_argument("--title", required=True); pr.add_argument("--body-file", required=True)
    q = p("request", who=False); q.add_argument("--pr", type=int, required=True)
    q.add_argument("--force", action="store_true", help="request even for a PR from outside the team")
    g = p("gate", who=False); g.add_argument("--pr", type=int, required=True)
    a = ap.parse_args()
    return globals()[f"cmd_{a.cmd.replace('-', '_')}"](a)


if __name__ == "__main__":
    sys.exit(main())
