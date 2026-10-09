#!/usr/bin/env python3
"""Team tooling. Everyone uses it to find work and to post, so every comment is
signed and tracked. Talks to GitHub through the `gh` CLI: each member's own
GitHub App token locally (see `token`), the workflow token in CI. Python 3.9+,
standard library only.

  team.py brief   --as ID                        your brief
  team.py tick    --as ID [--peek]               exit 0 = time to check, 3 = not yet
  team.py inbox   --as ID [--json|--count]       what needs you now
  team.py context --as ID --pr N                 a pull request, prepared for review
  team.py review  --as ID --pr N --file F.json   post your review
  team.py comment --as ID --on N (--body T | --body-file F) [--to "A, B"] [--needs-maintainer]
  team.py token   --as ID                        scheduler: a one-hour GitHub token for ID's App
  team.py request --pr N [--force]               CI: ask reviewers for a round
  team.py gate    --pr N                         CI: decide the review/gate status
"""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def excluded(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in CFG["diff_exclude_globs"])


def filter_diff(diff: str) -> tuple[str, list[str]]:
    """Drop lockfiles, generated and binary files from a unified diff."""
    kept, skipped, keep = [], [], True
    for line in diff.splitlines(keepends=True):
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
        if m:
            keep = not excluded(m.group(2))
            if not keep:
                skipped.append(m.group(2))
        if keep:
            kept.append(line)
    return "".join(kept), skipped


def enc(obj) -> str:
    # Standard alphabet on purpose: it has no "-", so a marker can never contain "--",
    # which would end the HTML comment early and print the data on the page.
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def dec(s: str):
    return json.loads(base64.b64decode(s.encode()))


# --------------------------------------------------------------------------- GitHub access

class GH:
    def __init__(self, repo: str | None = None):
        self.repo = repo or os.environ.get("GITHUB_REPOSITORY") or self._detect()

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
        cmd = ["gh", "api", "-X", method, path if path.startswith("/") else f"/repos/{self.repo}/{path}"]
        if accept:
            cmd += ["-H", f"Accept: {accept}"]
        if body is not None:
            cmd += ["--input", "-"]
        out = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                             capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError(f"gh api {method} {path}: {out.stderr.strip()[:300]}")
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
                items.append({"number": n, "kind": "pr", "title": t["title"], "why": why, "url": t["html_url"],
                              "next": (f"python3 team/team.py context --as {me} --pr {n}" if role == "reviewer"
                                       else f"gh pr view {n} --comments"), "action": f"{verb} PR #{n}"})
                seen.add(n)
        labels = {l["name"] for l in t.get("labels", [])}
        if (role == "implementer" and not is_pr and CFG["labels"]["ready"] in labels
                and not any(by(c) == me for c in comments)
                and labelled_by_human(gh.paged(f"issues/{n}/events"), CFG["labels"]["ready"])):
            items.append({"number": n, "kind": "issue", "title": t["title"], "why": "ready for implementation",
                          "url": t["html_url"], "action": f"Pick up issue #{n}", "next": f"gh issue view {n} --comments"})
            seen.add(n)
        for c in mention_needs(me, t, comments):
            if n in seen:
                break
            items.append({"number": n, "kind": "mention", "title": t["title"], "why": "addressed to you",
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


def cmd_context(a) -> int:
    gh = GH()
    pr = gh.get(f"pulls/{a.pr}")
    comments = gh.comments(a.pr)
    diff, skipped = filter_diff(gh.api(f"pulls/{a.pr}", accept="application/vnd.github.v3.diff"))
    commits = [c["commit"]["message"] for c in gh.paged(f"pulls/{a.pr}/commits")][-20:]
    prev = latest(comments, lambda c: kind(c) == "review" and by(c) == a.as_)
    s = [f"# Pull request #{a.pr}: {blind(pr['title'])}", f"Head commit: {pr['head']['sha'][:12]}",
         "", "## Description", "", blind(pr.get("body") or "") or "(none)"]
    if commits:
        s += ["", "## Commit messages", "", "\n---\n".join(blind(c) for c in commits)]
    if prev:
        s += ["", "## Your previous review", "", blind(prev["body"])]
        later = [c for c in comments if c["created_at"] > prev["updated_at"] and credible(c) and by(c) != a.as_
                 and (by(c) == "tim-drake" or addressed_to(c["body"], a.as_))]
        if later:
            s += ["", "## Replies since", ""] + [blind(c["body"]) + "\n---" for c in later]
    s += ["", "## Diff", ""]
    if skipped:
        s += [f"(Not shown by rule - lockfiles, generated, binary: {', '.join(skipped)})", ""]
    s += ["```diff", diff, "```", "",
          "## Reading more code",
          f"`git fetch origin pull/{a.pr}/head:pr-{a.pr}` then `git show pr-{a.pr}:<path>` to read any file at this commit. "
          "Do not check out or run the branch.", "",
          "## Submitting", f"Write your review JSON (format in your brief) to a file, then:",
          f"`python3 team/team.py review --as {a.as_} --pr {a.pr} --file <file>`"]
    print("\n".join(s))
    return 0


def cmd_review(a) -> int:
    if MEMBERS[a.as_]["role"] != "reviewer":
        sys.exit(f"{a.as_} is not a reviewer")
    gh = GH()
    payload = json.loads(Path(a.file).read_text())
    pr = gh.get(f"pulls/{a.pr}")
    files = {f["filename"] for f in gh.paged(f"pulls/{a.pr}/files")}
    prev = latest(gh.comments(a.pr), lambda c: kind(c) == "review" and by(c) == a.as_)
    pm = parse_marker(prev["body"]) if prev else {}
    r = build_review(a.as_, payload, files, pr["head"]["sha"], int(pm.get("round", 0)), pm.get("sha", ""))
    gh.upsert(a.pr, "review", a.as_, render_review(r))
    print(f"posted: {VERDICT_LABEL[r['verdict']]}, round {r['round']}, {len(r['findings'])} finding(s)"
          + (f"; dropped {len(r['dropped'])} citing files outside the change" if r["dropped"] else ""))
    return 0


def cmd_comment(a) -> int:
    m = MEMBERS[a.as_]
    body = Path(a.body_file).read_text() if a.body_file else (a.body or "")
    if not body.strip():
        sys.exit("empty comment")
    head = []
    if a.to:
        head.append(f"To: {a.to}")
    if a.needs_maintainer and not a.to:
        head.append("To: maintainer")
    sign = f"- {m['name']}"
    text = "\n".join([marker("comment", id=m["id"])] + head + ([""] if head else []) + [body.rstrip()]
                     + ([] if body.rstrip().endswith(sign) else ["", sign]))
    gh = GH()
    gh.api(f"issues/{a.on}/comments", "POST", {"body": text})
    if a.needs_maintainer:
        gh.api(f"issues/{a.on}/labels", "POST", {"labels": [CFG["labels"]["needs_maintainer"]]})
    print("posted")
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
    q = p("request", who=False); q.add_argument("--pr", type=int, required=True)
    q.add_argument("--force", action="store_true", help="request even for a PR from outside the team")
    g = p("gate", who=False); g.add_argument("--pr", type=int, required=True)
    a = ap.parse_args()
    return globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    sys.exit(main())
