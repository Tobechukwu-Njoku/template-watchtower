---
name: issue-to-pr
description: Implement one agreed GitHub issue end to end and open a ready pull request. Use when your inbox lists "Pick up issue #N", or when asked to implement an issue. Claims the issue, branches, reproduces or scopes, implements, verifies, then opens the PR.
---

# Issue to PR

The goal is not "produce a diff". It is: confirm the problem is real, make the smallest change that solves it, prove it by execution, then hand it to the team.

Track the steps below as a task list.

## 1. Understand and claim

```sh
gh issue view <N> --comments
```

Decide: bug or feature (`fix/` or `feat/`), the acceptance criteria, and what is out of scope. If any of that is unclear, ask instead of guessing:

```sh
python3 team/team.py comment --as tim-drake --on <N> --needs-maintainer --body "<the specific question>"
```

Otherwise claim it, so it leaves your inbox and the team knows it is taken:

```sh
python3 team/team.py comment --as tim-drake --on <N> --body "Picked up. Branch: <prefix>/<N>-<slug>."
```

## 2. Branch off fresh main, in your own worktree

```sh
git fetch origin
git worktree add -b <prefix>/<N>-<slug> ../wt-<N> origin/main
cd ../wt-<N>
```

## 3. Confirm - a hard gate

- **Bug:** reproduce it on current `main`, ideally as a failing test. If it does not reproduce, comment what you tried with `--needs-maintainer` and stop. Do not patch.
- **Feature:** confirm it is actually missing, then find the module that owns the behaviour and the patterns to follow.

Plan in proportion. A one-sentence change needs no plan. Multi-file or unfamiliar work gets a short plan (files, interfaces, out of scope, how it will be verified). For anything large, post the plan to the issue for the maintainer before building.

## 4. Implement

One concern. Grep for every symbol you call rather than trusting memory. For a bug, the failing test from step 3 now passes.

## 5. Verify

```sh
make ci
```

Run the full gate, not just your test. Keep the output - it goes in the PR. If it cannot pass on the merits, stop and report. Never weaken a check to get green.

## 6. Open the PR

Write the description from `.github/PULL_REQUEST_TEMPLATE.md` to `.pr-body.md`: Problem, Approach, Validation (real output), Risks. End with `Closes #<N>` and `- Tim Drake`.

```sh
git push -u origin HEAD
gh pr create --base main --title "<conventional subject>" --body-file .pr-body.md
```

Open it ready, not draft - that is what sends the review request. Attach screenshots for UI changes or say they are still needed.

## 7. Hand over

The reviewers pick it up on their next check. You will see it in your inbox again only if the gate fails or someone addresses you. Stop here.
