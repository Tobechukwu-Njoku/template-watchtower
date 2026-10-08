#!/usr/bin/env python3
"""Offline checks for panel.py. No network, no keys. Run: python3 .github/review/selftest.py"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import panel  # noqa: E402

DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import os
+password = os.environ["PW"]
 print("hi")
diff --git a/package-lock.json b/package-lock.json
--- a/package-lock.json
+++ b/package-lock.json
@@ -1 +1 @@
-{}
+{"a":1}
"""

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


# Blinding removes provenance, keeps substance.
body = """## Problem
Fix the thing.

## Provenance
- Tool: something

## Validation
make test -> 3 passed
Co-Authored-By: Someone <x@y>
🤖 Generated with [Claude Code](https://claude.com/claude-code)
<!-- models=github-models:openai/gpt-4.1 -->"""
b = panel.blind(body)
check("blind keeps problem and validation", "Fix the thing." in b and "3 passed" in b)
check("blind strips provenance section", "Tool: something" not in b)
check("blind strips trailers, tool banner and hidden comments",
      all(s not in b.lower() for s in ("co-authored-by", "claude", "models=")))

# Diff handling.
chunks, reviewed, skipped, cut = panel.chunk_diff(DIFF, 10_000)
check("lockfile excluded", skipped == ["package-lock.json"] and reviewed == ["src/app.py"])
big = "diff --git a/big.txt b/big.txt\n--- a/big.txt\n+++ b/big.txt\n" + "".join(
    f"@@ -{i},1 +{i},1 @@\n-{'x' * 900}\n+{'y' * 900}\n" for i in range(1, 200))
chunks, reviewed, _, cut = panel.chunk_diff(big, 8_000)
check("big file split into capped chunks", len(chunks) == panel.CONFIG["diff"]["max_chunks"])
check("every chunk keeps the file header", all(c.startswith("diff --git a/big.txt") for c in chunks))

# Review with a mocked reply: phantom-file findings dropped, verdict derived from findings.
reply = {"verdict": "approve", "summary": "Looks fine.", "findings": [
    {"severity": "major", "file": "src/app.py", "line": 2, "title": "Secret read at import", "detail": "d", "suggestion": "s"},
    {"severity": "blocker", "file": "src/ghost.py", "line": 9, "title": "Phantom", "detail": "d"}]}
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump(reply, f)
ctx = {"title": "feat: x", "body": body, "diff": DIFF, "commits": ["feat: x\n\nCo-Authored-By: A <a@b>"],
       "head_sha": "abcdef1234567890", "previous": "", "replies": [], "round": 1}
rv = panel.CONFIG["reviewers"][0]
r = panel.review_one(rv, ctx, mock=f.name)
check("phantom finding dropped", [x["title"] for x in r["findings"]] == ["Secret read at import"])
check("verdict follows findings, not the model's label", r["verdict"] == "request_changes")
md = panel.render(r)
check("comment carries marker and sign-off", md.startswith("<!-- panel-review:") and md.rstrip().endswith(f"- {rv['name']}"))
check("re-fed review is blind to model metadata", "models=" not in panel.blind(md) and "gpt" not in panel.blind(md))

# The prompt the reviewer sees contains no provenance.
prompt = panel.build_user_prompt(ctx, chunks[0], 1, 1, [])
check("prompt is blinded", "co-authored-by" not in prompt.lower() and "claude" not in prompt.lower())

# Persona files exist for every configured reviewer and never mention tooling.
banned = ("artificial intelligence", "language model", " llm", " ai ", "chatbot", "assistant", "prompt engineer")
for rv in panel.CONFIG["reviewers"]:
    text = (panel.PERSONAS / f"{rv['id']}.md").read_text().lower() + (panel.PERSONAS / "_team.md").read_text().lower()
    check(f"persona {rv['id']} exists and is framed as a colleague", not any(w in text for w in banned))

sys.exit(1 if failures else 0)
