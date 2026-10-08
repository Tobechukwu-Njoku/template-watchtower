---
name: issue-to-pr
disable-model-invocation: true
description: Implement one GitHub issue end to end and open a ready pull request. Invoke as /issue-to-pr <number|url>. Creates a branch, reproduces or scopes, implements, verifies, runs the team pre-review, then opens the PR.
---

# Issue to PR

The goal is not "produce a diff". It is: confirm the problem is real, make the smallest change that solves it, prove it by execution, let the team try to break it, then ship.

Track the steps below as a task list.

## 1. Understand

```sh
gh issue view "$ARGUMENTS" --json number,title,body,labels,comments,url
```

Decide: bug or feature (`fix/` or `feat/`), the acceptance criteria, and what is out of scope. If any of that is unclear, stop and ask the maintainer now.

## 2. Branch off fresh main

```sh
git fetch origin
git switch -c fix/<N>-<slug> origin/main   # feat/ for features
```

## 3. Confirm - a hard gate

- **Bug:** reproduce it on current `main`, ideally as a failing test. If it does not reproduce, stop and report what you tried. Do not patch.
- **Feature:** confirm it is actually missing, then find the module that owns the behaviour and the patterns to follow.

Plan in proportion. A one-sentence change needs no plan. Multi-file or unfamiliar work gets a short plan (files, interfaces, out of scope, how it will be verified) - and for anything large, confirm direction with the maintainer first.

## 4. Implement

One concern. Grep for every symbol you call rather than trusting memory. For a bug, the failing test from step 3 now passes.

## 5. Verify

```sh
make ci
```

Run the full gate, not just your test. Keep the output - it goes in the PR. If it cannot pass on the merits, stop and report. Never weaken a check to get green.

## 6. Pre-review

1. Write the draft PR description (template: `.github/PULL_REQUEST_TEMPLATE.md`) to `.pr-body.md`. Fill Problem, Approach, Validation, Risks.
2. Commit, then `make review-local`.
3. For each finding: open the cited file and line and confirm it is real. Then fix it, or write a rebuttal with evidence into the PR description's Pre-review section. Minor and nit findings are optional.
4. Commit fixes and `make review-again`. Cap at two rounds (three at most). If blocker/major findings still stand, stop and ask the maintainer.

## 7. Open the PR

```sh
git push -u origin HEAD
gh pr create --base main --title "<conventional subject>" --body-file .pr-body.md
```

The title is a Conventional Commit. The body ends with `Closes #<N>` and `- Tim Drake`. Attach screenshots for UI changes or say they are still needed.

## 8. Report back

PR link, two-line summary, verification result, pre-review outcome, open risks.
