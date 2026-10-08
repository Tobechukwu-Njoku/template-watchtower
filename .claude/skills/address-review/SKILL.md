---
name: address-review
description: Work through the team's review comments on an open pull request - verify each finding, fix or rebut it, reply, and push the next round. Use when a PR has review comments from Barbara, Lucius, Bruce or Victor, or the review/gate status is red.
---

# Address review

## 1. Read the round

```sh
gh pr view <N> --json number,headRefName,statusCheckRollup,url
gh pr view <N> --comments
```

Each reviewer leaves one comment, updated in place each round, with a verdict and numbered findings. James Gordon's comment summarises what blocks the merge.

## 2. Triage every blocker and major finding

For each one, in order:

1. Open the cited file and line. Is the finding real and reachable on the current code?
2. **Real:** fix it. Add or adjust a test that would have caught it.
3. **Not real, or already handled:** rebut it. Quote the code or output that shows why.
4. Minor and nit findings: fix if cheap, otherwise note them as follow-ups.

Do not gold-plate and do not add defensive code for cases that cannot happen.

## 3. Reply once, then push

Post one comment covering every blocker and major finding, so the next round sees your answers:

```
Round <R> responses

Barbara #1 (major) - fixed in <sha>: <one line>.
Bruce #2 (major) - not changing: <evidence>.
Victor #1 (minor) - follow-up issue #<M>.

- Tim Drake
```

Then `make ci`, commit, and push. The push starts the next round automatically. To re-run without a push, comment `/review`.

## 4. Know when to stop

Three rounds at most. If blocker or major findings still stand, stop and summarise for the maintainer: what is left, your view, and the options (fix, accept the risk with the override label, or narrow the PR). Never weaken a check to clear a finding.
