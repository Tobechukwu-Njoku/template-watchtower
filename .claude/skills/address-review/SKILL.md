---
name: address-review
description: Work through the team's review findings on an open pull request - verify each one, fix or rebut it, reply once, and push the next round. Use when your inbox lists "Address review on PR #N".
---

# Address review

## 1. Read the round

```sh
gh pr view <N> --comments
```

Each reviewer has one review comment, updated in place each round, with a verdict and numbered findings. James Gordon's gate comment lists what blocks the merge.

## 2. Triage every blocker and major finding

For each one, in order:

1. Open the cited file and line. Is the finding real and reachable on the current code?
2. **Real:** fix it, and add or adjust a test that would have caught it.
3. **Not real, or already handled:** rebut it. Quote the code or output that shows why.
4. Minor and nit findings: fix if cheap, otherwise list them as follow-ups.

Do not gold-plate, and do not add defensive code for cases that cannot happen.

## 3. Reply once, then push

Post one reply covering every blocker and major finding, addressed to the reviewers who raised them:

```sh
python3 team/team.py comment --as tim-drake --on <N> --to "Barbara Gordon, Bruce Wayne" --body-file reply.md
```

```
Round <R> responses

Barbara #1 (major) - fixed in <sha>: <one line>.
Bruce #2 (major) - not changing: <evidence>.
Victor #1 (minor) - follow-up issue #<M>.
```

Then `make ci`, commit and push. The push sends a new review request automatically.

If you only rebutted and changed no code, do not push. Your reply reaches the reviewers you addressed, and they update their reviews on their next check.

## 4. Know when to stop

Three rounds at most. If blocking findings still stand, stop and hand it to the maintainer: what is left, your view, and the options (fix, accept the risk with the `review-override` label, or narrow the PR).

```sh
python3 team/team.py comment --as tim-drake --on <N> --needs-maintainer --body-file summary.md
```

Never weaken a check to clear a finding.
