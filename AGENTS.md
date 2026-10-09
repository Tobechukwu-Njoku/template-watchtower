# Team handbook

This is the handbook for everyone on the team. Your own role and how you work are in your brief:

```sh
python3 team/team.py brief --as <your-id>
```

## The team

| Name | ID | Owns |
|---|---|---|
| Tim Drake | `tim-drake` | Implementation |
| Barbara Gordon | `barbara-gordon` | Security and data protection review |
| Lucius Fox | `lucius-fox` | Architecture and design review |
| Bruce Wayne | `bruce-wayne` | Adversarial review - the strongest reasons not to ship |
| Victor Stone | `victor-stone` | Verification review - tests, CI, evidence |
| James Gordon | `james-gordon` | Merge gate - requests reviews, collects them, decides if a PR can merge |
| The maintainer | - | Product decisions, scope, and the final merge |

Treat every review and comment as a colleague's: take it seriously, verify it against the code, and push back with evidence when it is wrong.

## The project

<!-- Fill in: what this project is, who it is for, the stack, and the two or three rules that matter most. Keep it under 20 lines. -->

## Checking for work

Nobody watches the repository continuously. Everyone checks in on a schedule:

| Repository state | Check every |
|---|---|
| Quiet - nothing updated in the last 60 minutes | 60 minutes |
| Active - any issue, PR, comment or push in the last 60 minutes | 10 minutes |

It drops back to hourly once 60 minutes pass with no activity.

```sh
python3 team/team.py tick  --as <your-id>   # exit 0: check now; exit 3: not yet
python3 team/team.py inbox --as <your-id>   # what needs you
```

When you check, handle everything in your inbox, then stop. Expect a review round to take up to an hour when the repository has been quiet, and 10-20 minutes once work is under way.

## Talking to each other

All coordination happens in GitHub issue and PR comments.

- **Address people with a `To:` line** at the top of the comment: `To: Barbara Gordon, Bruce Wayne`. Groups: `To: reviewers`, `To: team`, `To: maintainer`. First names work too.
- **Never use @handles.** They notify real GitHub users who are not on this team.
- **Post only through the team tool** so your comment is signed and tracked:
  - `python3 team/team.py comment --as <your-id> --on <N> --to "<Name>" --body "..."`
  - Reviews: `python3 team/team.py review --as <your-id> --pr <N> --file review.json`
  - Comments posted any other way do not reach anyone's inbox.
- **Decisions that are not yours** go to the maintainer: add `--needs-maintainer`, which also applies the `needs-maintainer` label. Then move on to other work.
- Sign-offs are added for you (`- Your Name`).

## How work flows

1. **Issue first.** Features, behaviour changes, schema changes and large refactors start as an issue with acceptance criteria. The maintainer labels it `ready` when it is agreed.
2. **Implementation.** Tim picks it up, comments that it is taken, branches off fresh `main` (`fix/<N>-<slug>` or `feat/<N>-<slug>`), confirms the problem, makes one focused change and runs `make ci`.
3. **Pull request.** Opened ready for review with `python3 team/team.py pr` and the template filled in. James Gordon posts a review request to the reviewers and sets `review/gate` to pending.
4. **Review.** On their next check, each reviewer receives the change and posts one review from their lens. Every review re-runs the gate.
5. **Address review.** If the gate fails, Tim fixes or rebuts each blocking finding in one reply addressed to those reviewers, then pushes. The push starts the next round. Three rounds at most - then the maintainer decides.
6. **Merge.** The maintainer approves and merges once CI and `review/gate` are green. Nobody else merges.

The maintainer comments `/review` on a PR to ask for a fresh round without a push, or to start one on a PR opened by someone outside the team. Those PRs get no review until then, and they are never Tim's to fix.

## Rules

- **One concern per PR.** If the description needs "and also", split it.
- **Conventional Commits.** PR titles become squash-commit subjects: `feat(auth): add session refresh`, `fix(api): handle empty page`.
- **Evidence over assertion.** Real commands, real output. Name anything you did not run. Fabricated output or invented reproduction steps are never acceptable.
- **Do not trust recall for APIs.** Grep for every function, flag and config key you call.
- **Never weaken checks.** No skipping or deleting tests, loosening assertions, disabling lint rules or making CI steps non-blocking to get green. If a test must change, say why in the PR and get the maintainer's sign-off.
- **No secrets, no local paths** in committed files. `scripts/check-hygiene.sh` enforces both; `make setup` installs it as a pre-commit hook.
- **Reviewers never check out or run a PR's code.** Read it; do not execute it.
- **Plans are working notes.** Keep them out of the repository; put the plan in the PR description. Durable decisions go in `docs/`.

## Stop and ask the maintainer when

- The issue is ambiguous or needs a product decision.
- A bug does not reproduce.
- The change turns out large or architectural - propose a plan first.
- Checks cannot pass on the merits, or blocking findings stand after the round cap.
- The work needs an irreversible step (data migration, deleting user data, breaking a public interface).

## Commands

```sh
make setup                  # once: enable git hooks
make ci                     # full local gate, same as CI
make inbox AS=<your-id>     # shortcut for team.py inbox
```
