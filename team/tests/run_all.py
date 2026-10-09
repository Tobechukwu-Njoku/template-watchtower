#!/usr/bin/env python3
"""Every team test: the offline self-test, then the end-to-end scenarios against fake
GitHub, a fake Antigravity connector and a stand-in Claude Code. No network, no
accounts. CI runs this as "Team self-test"; locally: make test.

The end-to-end scheduler test clones the repository's committed HEAD for Tim, so commit
before running it locally."""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITES = ["team/selftest.py", "team/tests/test_token.py", "team/tests/test_create_apps.py",
          "team/tests/e2e_identities.py", "team/tests/e2e_reviewers.py", "team/tests/e2e_scheduler.py"]

failed = []
for suite in SUITES:
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / suite)], cwd=ROOT, capture_output=True, text=True)
    lines = r.stdout.splitlines()
    oks = sum(l.startswith("ok ") for l in lines)
    bad = [l for l in lines if l.startswith("FAIL")]
    status = "ok" if r.returncode == 0 else "FAILED"
    print(f"{status:7} {suite:34} {oks:3} checks  {time.time() - t0:5.1f}s")
    if r.returncode != 0:
        failed.append(suite)
        for l in bad or lines[-15:]:
            print("        " + l)
        if r.stderr.strip():
            print("        " + r.stderr.strip().replace("\n", "\n        ")[-1500:])
print(f"\n{len(SUITES) - len(failed)} of {len(SUITES)} suites passed")
sys.exit(1 if failed else 0)
