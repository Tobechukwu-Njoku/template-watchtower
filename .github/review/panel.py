#!/usr/bin/env python3
"""Review panel: persona reviewers and the merge gate.

Standard library only, so it runs on any runner or laptop with Python 3.9+.

  panel.py review --reviewer barbara-gordon --pr 12 --post      # CI, one reviewer
  panel.py review --reviewer all --local --base origin/main     # laptop, whole panel
  panel.py gate --pr 12                                          # CI, aggregate + status
  panel.py gate --pr 12 --pending                                # CI, mark status pending
  panel.py models                                                # list GitHub Models catalog

Reviewers are given a blinded view of the change: provenance sections, tool
trailers and hidden metadata are stripped (see "blind" in config.json), so the
review is about the code, not about who or what produced it.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
PERSONAS = HERE / "personas"
API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
SEVERITIES = ["blocker", "major", "minor", "nit"]
CHARS_PER_TOKEN = 3.5  # conservative for code
REVIEW_MARK = "<!-- panel-review:{id} "
GATE_MARK = "<!-- panel-gate -->"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- HTTP

def http(method: str, url: str, headers: dict, body=None, timeout: int = 180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.headers), raw


def gh_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


class GitHub:
    def __init__(self, repo: str):
        self.repo = repo
        self.h = {
            "Authorization": f"Bearer {gh_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "review-panel",
        }

    def get(self, path: str, accept: str | None = None):
        h = dict(self.h)
        if accept:
            h["Accept"] = accept
        _, _, raw = http("GET", f"{API}/repos/{self.repo}{path}", h)
        return raw if accept else json.loads(raw)

    def paged(self, path: str) -> list:
        out, page = [], 1
        while True:
            sep = "&" if "?" in path else "?"
            batch = self.get(f"{path}{sep}per_page=100&page={page}")
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def send(self, method: str, path: str, body: dict):
        _, _, raw = http(method, f"{API}/repos/{self.repo}{path}", self.h, body)
        return json.loads(raw) if raw else {}

    def upsert_comment(self, pr: int, marker: str, body: str) -> None:
        for c in self.paged(f"/issues/{pr}/comments"):
            if c["body"].startswith(marker):
                self.send("PATCH", f"/issues/comments/{c['id']}", {"body": body})
                return
        self.send("POST", f"/issues/{pr}/comments", {"body": body})


# --------------------------------------------------------------------------- blinding

def blind(text: str) -> str:
    """Strip provenance so reviewers judge the change, not its origin."""
    if not text:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    for section in CONFIG["blind"]["strip_sections"]:
        text = re.sub(rf"(?ims)^#+\s*{re.escape(section)}\b.*?(?=^#+\s|\Z)", "", text)
    for pat in CONFIG["blind"]["strip_line_patterns"]:
        text = re.sub(pat, "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# --------------------------------------------------------------------------- diff handling

def split_diff(diff: str) -> list[tuple[str, str]]:
    """Return [(path, file_block)] for each file in a unified git diff."""
    blocks, cur_path, cur = [], None, []
    for line in diff.splitlines(keepends=True):
        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line.rstrip("\n"))
        if m:
            if cur_path is not None:
                blocks.append((cur_path, "".join(cur)))
            cur_path, cur = m.group(2), [line]
        elif cur_path is not None:
            cur.append(line)
    if cur_path is not None:
        blocks.append((cur_path, "".join(cur)))
    return blocks


def excluded(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in CONFIG["diff"]["exclude_globs"])


def split_big_block(block: str, budget: int) -> list[str]:
    """Split one file's diff by hunk, repeating the file header on each piece."""
    parts = re.split(r"(?m)^(?=@@ )", block)
    header, hunks = parts[0], parts[1:]
    pieces, cur = [], header
    for h in hunks:
        if len(cur) + len(h) > budget and cur != header:
            pieces.append(cur)
            cur = header
        cur += h[: max(budget - len(header), 500)]
    pieces.append(cur)
    return pieces


def chunk_diff(diff: str, budget_chars: int) -> tuple[list[str], list[str], list[str], list[str]]:
    """Pack file diffs into chunks. Returns (chunks, reviewed_paths, skipped_by_glob, cut_for_size)."""
    files = split_diff(diff)
    skipped = [p for p, _ in files if excluded(p)]
    files = [(p, b) for p, b in files if not excluded(p)]
    pieces: list[tuple[str, str]] = []
    for p, b in files:
        if len(b) > budget_chars:
            pieces += [(p, s) for s in split_big_block(b, budget_chars)]
        else:
            pieces.append((p, b))
    chunks, paths, cur, cur_paths = [], [], "", []
    for p, b in pieces:
        if cur and len(cur) + len(b) > budget_chars:
            chunks.append(cur)
            paths.append(cur_paths)
            cur, cur_paths = "", []
        cur += b
        cur_paths.append(p)
    if cur:
        chunks.append(cur)
        paths.append(cur_paths)
    limit = CONFIG["diff"]["max_chunks"]
    cut = sorted({p for ps in paths[limit:] for p in ps} - {p for ps in paths[:limit] for p in ps})
    reviewed = sorted({p for ps in paths[:limit] for p in ps})
    return chunks[:limit], reviewed, skipped, cut


# --------------------------------------------------------------------------- model calls

def models_for(reviewer: dict) -> list[dict]:
    """A reviewer names a tier (shared model chain) or lists its own models."""
    return reviewer.get("models") or CONFIG["tiers"][reviewer["tier"]]


def has_key(m: dict) -> bool:
    env = CONFIG["providers"][m["provider"]]["key_env"]
    return bool(os.environ.get(env) or (env == "GITHUB_TOKEN" and gh_token()))


def call_model(provider: dict, model: str, system: str, user: str, effort: str | None = None) -> str:
    key = os.environ.get(provider["key_env"], "")
    if provider["key_env"] == "GITHUB_TOKEN" and not key:
        key = gh_token()
    if not key:
        raise RuntimeError(f"no key in ${provider['key_env']}")
    max_out = provider["max_output_tokens"]
    if provider["type"] == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "review-panel"}
        body = {"model": model, "max_tokens": max_out, "system": system,
                "messages": [{"role": "user", "content": user}], "temperature": 0.2}
        _, _, raw = http("POST", provider["url"], headers, body)
        return "".join(b.get("text", "") for b in json.loads(raw)["content"])
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "review-panel"}
    body = {"model": model, "temperature": 0.2, "max_tokens": max_out,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"}}
    if effort:
        body["reasoning_effort"] = effort  # Gemini maps this to thinking_level
    for attempt in range(3):
        try:
            _, _, raw = http("POST", provider["url"], headers, body)
            return json.loads(raw)["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")  # some models reject JSON mode; ask in prose instead
                continue
            if e.code == 400 and "reasoning_effort" in body:
                body.pop("reasoning_effort")
                continue
            wait = int(e.headers.get("Retry-After", "0") or 0)
            if e.code == 429 and 0 < wait <= 30 and attempt < 2:
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    raise RuntimeError("exhausted retries")


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start : end + 1])


def ask(reviewer: dict, system: str, user: str, mock: str | None):
    if mock:
        return parse_json(Path(mock).read_text()), "mock"
    errors = []
    for m in models_for(reviewer):
        provider = CONFIG["providers"][m["provider"]]
        if (len(system) + len(user)) / CHARS_PER_TOKEN > provider["max_input_tokens"]:
            errors.append(f"{m['provider']}:{m['model']} -> skipped, input larger than its limit")
            log(f"[{reviewer['id']}] {errors[-1]}")
            continue
        try:
            reply = call_model(provider, m["model"], system, user, m.get("reasoning_effort"))
            return parse_json(reply), f"{m['provider']}:{m['model']}"
        except Exception as e:  # fall through to the next model in the chain
            errors.append(f"{m['provider']}:{m['model']} -> {e}")
            log(f"[{reviewer['id']}] {errors[-1]}")
    raise RuntimeError("; ".join(errors))


# --------------------------------------------------------------------------- review

def gather_pr(gh: GitHub, pr: int, reviewer_id: str) -> dict:
    meta = gh.get(f"/pulls/{pr}")
    diff = gh.get(f"/pulls/{pr}", accept="application/vnd.github.v3.diff")
    commits = [c["commit"]["message"] for c in gh.paged(f"/pulls/{pr}/commits")][-20:]
    comments = gh.paged(f"/issues/{pr}/comments")
    marker = REVIEW_MARK.format(id=reviewer_id)
    prev = next((c for c in reversed(comments) if c["body"].startswith(marker)), None)
    prev_round, prev_sha = 0, ""
    replies = []
    if prev:
        m = re.search(r"round=(\d+) sha=(\w+)", prev["body"])
        if m:
            prev_round, prev_sha = int(m.group(1)), m.group(2)
        for c in comments:
            if (c["created_at"] > prev["created_at"] and "<!-- panel-" not in c["body"]
                    and c.get("author_association") in ("OWNER", "MEMBER", "COLLABORATOR")):
                replies.append(c["body"])
    head = meta["head"]["sha"]
    return {
        "title": meta["title"], "body": meta.get("body") or "", "diff": diff, "commits": commits,
        "head_sha": head, "previous": prev["body"] if prev else "", "replies": replies,
        "round": prev_round if prev_sha == head[:12] else prev_round + 1,
    }


def gather_local(base: str, body_file: str | None, previous: str | None, reviewer_id: str) -> dict:
    run = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout
    diff = run("diff", "--no-color", f"{base}...HEAD")
    log_out = run("log", "--format=%B%x00", f"{base}..HEAD")
    commits = [c.strip() for c in log_out.split("\x00") if c.strip()]
    title = commits[-1].splitlines()[0] if commits else "(uncommitted)"
    prev_text = ""
    if previous:  # a directory of <reviewer>.md files from the last local round, or one file
        p = Path(previous)
        p = p / f"{reviewer_id}.md" if p.is_dir() else p
        prev_text = p.read_text() if p.exists() else ""
    return {
        "title": title, "body": Path(body_file).read_text() if body_file else "",
        "diff": diff, "commits": commits, "head_sha": run("rev-parse", "HEAD").strip(),
        "previous": prev_text, "replies": [],
        "round": int(m.group(1)) + 1 if (m := re.search(r"round=(\d+)", prev_text)) else 1,
    }


def build_user_prompt(ctx: dict, chunk: str, part: int, parts: int, other_files: list[str]) -> str:
    cap = CONFIG["diff"]["description_max_chars"]
    s = [f"## Pull request\n\nTitle: {blind(ctx['title'])}\n\nDescription:\n{blind(ctx['body'])[:cap] or '(none)'}"]
    if ctx["commits"]:
        msgs = "\n---\n".join(blind(c) for c in ctx["commits"])[:1500]
        s.append(f"## Commit messages\n\n{msgs}")
    if ctx["previous"]:
        s.append(f"## Your previous review (round {ctx['round'] - 1})\n\n{blind(ctx['previous'])[:2500]}")
        if ctx["replies"]:
            s.append("## Author replies since then\n\n" + "\n---\n".join(blind(r) for r in ctx["replies"])[:2000])
    if parts > 1:
        s.append(f"## Note\n\nThe diff is large, so you are seeing part {part} of {parts}. "
                 f"Other parts cover: {', '.join(other_files[:40]) or 'n/a'}. Review only what is shown.")
    s.append(f"## Diff\n\n```diff\n{chunk}\n```")
    return "\n\n".join(s)


def review_one(reviewer: dict, ctx: dict, mock: str | None = None) -> dict:
    system = (PERSONAS / "_team.md").read_text() + "\n\n" + (PERSONAS / f"{reviewer['id']}.md").read_text()
    # Size chunks for the first model that has a key; smaller fallbacks are skipped if a chunk will not fit.
    chain = [m for m in models_for(reviewer) if has_key(m)] or models_for(reviewer)
    budget_tokens = CONFIG["providers"][chain[0]["provider"]]["max_input_tokens"]
    fixed = len(system) + CONFIG["diff"]["description_max_chars"] + 6500  # prompt, description, history
    budget_chars = max(int(budget_tokens * CHARS_PER_TOKEN) - fixed, 6000)
    chunks, reviewed, skipped, cut = chunk_diff(ctx["diff"], budget_chars)

    result = {"reviewer": reviewer["id"], "name": reviewer["name"], "focus": reviewer["focus"],
              "round": max(ctx["round"], 1), "head_sha": ctx["head_sha"], "findings": [], "resolved": [],
              "summaries": [], "verdicts": [], "models": [], "skipped_files": skipped, "unreviewed_files": cut}
    if not chunks:
        result.update(verdict="approve", summary="No reviewable changes (only excluded or binary files).")
        return result
    try:
        all_files = reviewed
        for i, chunk in enumerate(chunks, 1):
            here = {p for p, _ in split_diff(chunk)}
            reply, model = ask(reviewer, system, build_user_prompt(ctx, chunk, i, len(chunks),
                               [p for p in all_files if p not in here]), mock)
            result["models"].append(model)
            result["summaries"].append(str(reply.get("summary", "")).strip())
            result["verdicts"].append(reply.get("verdict", "comment"))
            result["resolved"] += [str(r) for r in reply.get("resolved", []) or []]
            for f in reply.get("findings", []) or []:
                f["severity"] = f.get("severity", "minor") if f.get("severity") in SEVERITIES else "minor"
                if f.get("file") in here:  # drop findings that cite files outside the diff
                    result["findings"].append(f)
                else:
                    result.setdefault("dropped", []).append(f.get("title", "?"))
    except Exception as e:
        result.update(verdict="error", summary=f"Review could not complete: {e}"[:600])
        return result

    if cut:
        result["findings"].append({"severity": "major", "file": cut[0], "line": 0,
            "title": "Change too large to review in full",
            "detail": f"{len(cut)} file(s) did not fit in the review budget: {', '.join(cut[:15])}.",
            "suggestion": "Split this into smaller pull requests, one concern each."})
    seen, unique = set(), []
    for f in result["findings"]:
        k = (f.get("file"), f.get("line"), str(f.get("title", "")).lower())
        if k not in seen:
            seen.add(k)
            unique.append(f)
    unique.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    result["findings"] = unique
    blocking = any(f["severity"] in CONFIG["gate"]["blocking_severities"] for f in unique)
    # The verdict follows the findings, not the model's own label, so the two cannot disagree.
    if blocking:
        result["verdict"] = "request_changes"
    elif any(f["severity"] == "minor" for f in unique):
        result["verdict"] = "comment"
    else:
        result["verdict"] = "approve"
    result["summary"] = " ".join(s for s in result["summaries"] if s)
    return result


VERDICT_LABEL = {"approve": "Approved", "request_changes": "Changes requested",
                 "comment": "Comments, not blocking", "error": "Review did not complete"}


def render(r: dict) -> str:
    sha = r["head_sha"][:12]
    out = [f"{REVIEW_MARK.format(id=r['reviewer'])}round={r['round']} sha={sha} -->",
           f"<!-- models={','.join(r.get('models', []))} -->",
           f"### {r['focus']}",
           f"**{VERDICT_LABEL[r['verdict']]}** · round {r['round']} · `{sha[:7]}`", "", r["summary"] or ""]
    if r["findings"]:
        out += ["", "| Severity | Finding | Where |", "|---|---|---|"]
        for f in r["findings"]:
            out.append(f"| {f['severity']} | {str(f.get('title', '')).replace('|', '/')} | `{f.get('file')}:{f.get('line', '')}` |")
        out.append("")
        for n, f in enumerate(r["findings"], 1):
            out += [f"**{n}. [{f['severity']}] {f.get('title', '')}** - `{f.get('file')}:{f.get('line', '')}`",
                    "", str(f.get("detail", "")).strip()]
            if f.get("suggestion"):
                out += ["", f"Suggestion: {str(f['suggestion']).strip()}"]
            out.append("")
    if r.get("resolved"):
        out += ["", "Resolved since last round:"] + [f"- {x}" for x in r["resolved"]]
    if r.get("skipped_files"):
        out += ["", f"<sub>Not reviewed by rule (lockfiles, generated, binary): {len(r['skipped_files'])} file(s).</sub>"]
    out += ["", f"- {r['name']}"]
    return "\n".join(out)


def cmd_review(a) -> int:
    reviewers = CONFIG["reviewers"] if a.reviewer == "all" else [r for r in CONFIG["reviewers"] if r["id"] == a.reviewer]
    if not reviewers:
        log(f"unknown reviewer {a.reviewer}")
        return 2
    gh = None if a.local else GitHub(a.repo)
    worst = 0
    for rv in reviewers:
        ctx = gather_local(a.base, a.body_file, a.previous, rv["id"]) if a.local else gather_pr(gh, a.pr, rv["id"])
        r = review_one(rv, ctx, a.mock_response)
        md = render(r)
        if a.out:
            out = Path(a.out if len(reviewers) == 1 else f"{a.out.rstrip('/')}/{rv['id']}.json")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(r, indent=2))
            if a.local:
                out.with_suffix(".md").write_text(md)
        if a.post and gh:
            gh.upsert_comment(a.pr, REVIEW_MARK.format(id=rv["id"]), md)
        print(md if a.local else json.dumps({"reviewer": rv["id"], "verdict": r["verdict"]}))
        print()
        if r["verdict"] in ("request_changes", "error"):
            worst = 1
    return worst if a.local else 0  # in CI the gate decides


# --------------------------------------------------------------------------- gate

def set_status(gh: GitHub, sha: str, state: str, desc: str) -> None:
    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{gh.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = {"state": state, "context": CONFIG["gate"]["status_context"], "description": desc[:139]}
    if run_url:
        body["target_url"] = run_url
    gh.send("POST", f"/statuses/{sha}", body)


def cmd_gate(a) -> int:
    g = CONFIG["gate"]
    gh = GitHub(a.repo)
    meta = gh.get(f"/pulls/{a.pr}")
    sha = meta["head"]["sha"]
    if a.pending:
        set_status(gh, sha, "pending", "Review panel is reading the change")
        return 0
    labels = {l["name"] for l in meta.get("labels", [])}

    rows, failing, rounds = [], [], []
    for rv in CONFIG["reviewers"]:
        p = Path(a.verdicts) / f"{rv['id']}.json"
        if not p.exists():
            rows.append(f"| {rv['name']} | {rv['focus']} | missing | - |")
            failing.append(f"{rv['name']}: no verdict")
            continue
        r = json.loads(p.read_text())
        if r.get("head_sha") != sha:
            failing.append(f"{rv['name']}: reviewed an older commit")
        rounds.append(r.get("round", 1))
        counts = {s: sum(1 for f in r["findings"] if f["severity"] == s) for s in SEVERITIES}
        tally = ", ".join(f"{n} {s}" for s, n in counts.items() if n) or "none"
        rows.append(f"| {rv['name']} | {rv['focus']} | {VERDICT_LABEL[r['verdict']]} | {tally} |")
        if r["verdict"] == "error" and g["fail_on_reviewer_error"]:
            failing.append(f"{rv['name']}: review did not complete")
        if any(f["severity"] in g["blocking_severities"] for f in r["findings"]):
            failing.append(f"{rv['name']}: blocking findings")

    # Provenance is for humans only - reviewers never see this comment.
    trailers = set()
    for c in gh.paged(f"/pulls/{a.pr}/commits"):
        trailers |= set(re.findall(r"(?im)^co-authored-by:\s*(.+)$", c["commit"]["message"]))

    overridden = g["override_label"] in labels
    capped = failing and rounds and max(rounds) >= g["max_rounds"]
    if overridden:
        state, headline = "success", f"Overridden by maintainer label `{g['override_label']}`."
    elif failing:
        state = "failure"
        headline = ("Round cap reached with blocking findings. This needs a human decision: fix, "
                    f"rebut in a comment, or apply `{g['override_label']}`." if capped
                    else "Not ready to merge. Address or rebut the blocking findings and push again.")
    else:
        state, headline = "success", "Cleared to merge once CI is green."

    body = [GATE_MARK, "### Review panel", f"**{headline}**", "",
            "| Reviewer | Lens | Verdict | Findings |", "|---|---|---|---|", *rows]
    if failing and not overridden:
        body += ["", "Blocking:"] + [f"- {x}" for x in failing]
    if trailers:
        body += ["", "<details><summary>Provenance (hidden from reviewers)</summary>", ""]
        body += [f"- Co-authored-by: {t.strip()}" for t in sorted(trailers)] + ["", "</details>"]
    body += ["", f"- {g['name']}"]
    gh.upsert_comment(a.pr, GATE_MARK, "\n".join(body))
    set_status(gh, sha, state, headline)
    print(headline)
    return 0 if state == "success" else 1


# --------------------------------------------------------------------------- models

def cmd_models(_a) -> int:
    """Show who uses what, and check configured model IDs against the providers' live lists."""
    print("Assignments:")
    for rv in CONFIG["reviewers"]:
        chain = " -> ".join(f"{m['model']}{' (' + m['reasoning_effort'] + ')' if m.get('reasoning_effort') else ''}"
                            for m in models_for(rv))
        print(f"  {rv['name']:<16} {rv.get('tier', 'custom'):<6} {chain}")
    available: dict[str, set] = {}
    try:
        _, _, raw = http("GET", "https://models.github.ai/catalog/models",
                         {"Authorization": f"Bearer {gh_token()}", "User-Agent": "review-panel"})
        available["github-models"] = {m["id"] for m in json.loads(raw)}
    except Exception as e:
        log(f"github-models catalog: {e}")
    if os.environ.get("GEMINI_API_KEY"):
        try:
            _, _, raw = http("GET", "https://generativelanguage.googleapis.com/v1beta/openai/models",
                             {"Authorization": f"Bearer {os.environ['GEMINI_API_KEY']}", "User-Agent": "review-panel"})
            available["gemini"] = {m["id"].removeprefix("models/") for m in json.loads(raw)["data"]}
        except Exception as e:
            log(f"gemini models: {e}")
    print("\nConfigured model IDs:")
    seen = set()
    for chain in list(CONFIG["tiers"].values()) + [r["models"] for r in CONFIG["reviewers"] if r.get("models")]:
        for m in chain:
            key = (m["provider"], m["model"])
            if key in seen:
                continue
            seen.add(key)
            ok = available.get(m["provider"])
            mark = "unchecked" if ok is None else ("ok" if m["model"] in ok else "NOT FOUND")
            print(f"  {mark:<10} {m['provider']}:{m['model']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    r = sub.add_parser("review")
    r.add_argument("--reviewer", required=True, help="reviewer id from config.json, or 'all'")
    r.add_argument("--pr", type=int)
    r.add_argument("--repo", default=repo)
    r.add_argument("--local", action="store_true", help="review the current branch against --base")
    r.add_argument("--base", default="origin/main")
    r.add_argument("--body-file", help="local: draft PR description to include")
    r.add_argument("--previous", help="local: directory (or file) holding the last round's <reviewer>.md")
    r.add_argument("--out", help="write verdict JSON here (a directory when --reviewer all)")
    r.add_argument("--post", action="store_true", help="CI: create or update the PR comment")
    r.add_argument("--mock-response", help="testing: use this JSON file instead of calling a model")

    g = sub.add_parser("gate")
    g.add_argument("--pr", type=int, required=True)
    g.add_argument("--repo", default=repo)
    g.add_argument("--verdicts", default="verdicts")
    g.add_argument("--pending", action="store_true")

    sub.add_parser("models")
    a = ap.parse_args()
    if a.cmd == "review" and not a.local and not (a.pr and a.repo):
        ap.error("review needs --pr and --repo (or --local)")
    return {"review": cmd_review, "gate": cmd_gate, "models": cmd_models}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
