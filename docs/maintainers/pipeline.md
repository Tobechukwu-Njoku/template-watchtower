# Watchtower - maintainer guide

This file is for you, not the team. `.claude/settings.json` and `.geminiignore` keep the agents out of `docs/maintainers/` so the framing stays intact.

## New project from this template

1. On GitHub: **Use this template -> Create a new repository**. Public if you want the merge gate enforced on the free plan.
2. Clone it and run `scripts/setup-github.sh <repo-name> --public`: merge settings, labels, ruleset, security features.
3. Update `.github/CODEOWNERS` if the owner differs, fill in "The project" in `AGENTS.md`, and rewrite `README.md`.
4. Set up the agents on your machine - see [Running the team](#running-the-team).

## The idea

Every participant is an AI agent framed as a human colleague with one lens. Nothing in the repository tells them they are AIs, or that the code under review was machine-written:

1. **Better reasoning.** A reviewer playing "senior engineer with a reputation" behaves differently from one playing "helpful assistant". The briefs push toward concrete, evidence-backed findings and away from both flattery and over-flagging.
2. **No poisoned well.** Reviewers told "this is AI-generated code" hunt for stereotypical AI faults. Reviewers told nothing judge the diff on its merits.

Provenance is kept for you only. `team.py context`, which reviewers use to read a PR, strips the **Provenance** section, `Co-Authored-By` trailers, tool banners and hidden comments. James Gordon's gate comment lists the trailers in a collapsed block for you.

## Team and models

| Name | Role | Tool and model | Woken by |
|---|---|---|---|
| Tim Drake | Implementer | Claude Code - Claude Opus 5.5 (Ultra) | `ready` issues, failed gate, `To:` mentions |
| Barbara Gordon | Security and data protection | Gemini CLI - Gemini 3.1 Pro, High thinking | Review requests, `To:` mentions |
| Lucius Fox | Architecture and design | Gemini CLI - Gemini 3.1 Pro, High thinking | Review requests, `To:` mentions |
| Bruce Wayne | Adversarial review | Gemini CLI - Gemini 3.1 Pro, High thinking | Review requests, `To:` mentions |
| Victor Stone | Verification and tests | Gemini CLI - Gemini 3.8 Flash, High thinking | Review requests, `To:` mentions |
| James Gordon | Merge gate | GitHub Actions - no model | New commits, each posted review, `/review`, override label |

- **Claude writes, Gemini reviews.** No model judges output from its own family.
- **Pro on judgement-heavy lenses**, Flash on Victor, whose checks are more mechanical.
- **The gate has no model**, so the merge decision is reproducible and cannot be talked round.
- No API keys anywhere. Each agent runs in its own CLI under your subscription and talks to GitHub through your `gh` login.

## How they communicate

Everything happens in issue and PR comments, through `team/team.py`:

- Each comment starts with a hidden marker (`<!-- team:comment id=bruce-wayne -->`) and ends with a sign-off. All agents post as your GitHub account, so the marker - not the author - says who wrote it.
- People are addressed with a `To:` line (`To: Barbara Gordon`, `To: reviewers`, `To: maintainer`). Never @handles: those notify real GitHub users.
- Reviews carry their verdict and findings encoded in the marker, so the gate reads them without parsing prose.
- Only comments from OWNER/MEMBER/COLLABORATOR accounts (and the workflow bot) count. A stranger cannot forge a review on a public repo.

**You talk to them the same way.** Comment `To: Tim Drake` on an issue or PR, or label an issue `ready` to hand it to Tim. When an agent needs you it comments `To: maintainer` and adds the `needs-maintainer` label - filter on that label to find your queue.

## Check-in schedule

| Repository state | Each member checks every |
|---|---|
| Quiet - nothing updated in the last 60 minutes | 60 minutes |
| Active - any issue, PR, comment or push in the last 60 minutes | 10 minutes |

Drops back to hourly after 60 quiet minutes. All numbers are in `team/config.json` under `cadence`.

**Adjustment from "ask each agent to check":** having every agent wake on a timer and look for itself would spend model time on every empty check - up to 6 agents x 6 checks an hour while active. Instead `scripts/watch.sh` runs every 10 minutes from cron or launchd, applies the schedule (`team.py tick`), asks `team.py inbox` whether that member has anything, and only then launches the agent. An idle check costs two GitHub API calls and no agent time. The agents still see the schedule in `AGENTS.md`, so if you prefer to run them from their own scheduled tasks instead, they follow the same rules.

Expected latency: up to an hour for the first response when the repo has been quiet, 10-20 minutes per step once work is under way. A full review round usually lands within one active cycle, because the review request itself makes the repo active.

## Running the team

On the machine that will host the agents:

```sh
# 1. Clones: one for Tim to work in, one read-mostly clone for the reviewers
git clone https://github.com/<owner>/<repo> ~/code/<repo>-tim
git clone https://github.com/<owner>/<repo> ~/code/<repo>-review

# 2. Launch config
mkdir -p ~/.watchtower
cp ~/code/<repo>-review/docs/maintainers/agents.example.json ~/.watchtower/agents.json
#    edit: replace PROJECT with <repo>, check model IDs and CLI flags against your installed versions

# 3. Schedule (crontab -e). Every 10 minutes; watch.sh decides whether it is time.
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh tim-drake
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh barbara-gordon
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh lucius-fox
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh bruce-wayne
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh victor-stone
```

Requirements: `gh auth login` (used by everyone), `claude` and `gemini` CLIs logged in. Cron jobs get a minimal `PATH` - add a `PATH=...` line at the top of the crontab that includes `gh`, `claude`, `gemini` and `python3`. On macOS, give `cron` Full Disk Access or use a launchd agent. Logs: `~/.watchtower/logs/`.

Set **High** thinking in each tool's own settings - the CLI flags above choose the model only.

Test one member by hand: `scripts/watch.sh barbara-gordon; tail ~/.watchtower/logs/*barbara-gordon.log`. To force a check, delete `~/.watchtower/<owner>__<repo>/<id>.json`.

## Gate rules

- `review/gate` is **pending** until all four reviewers have reviewed the current head commit, **failure** as soon as any review has a `blocker` or `major` finding, **success** when all four are in and clean.
- Reviews of an older commit do not count; every push requests a new round.
- The verdict is computed from the findings, so a reviewer cannot "approve" while listing a blocker.
- Findings citing files outside the PR are dropped as phantoms.
- After 3 rounds with blockers standing, the gate says it needs your decision.
- **Your override:** the `review-override` label turns the gate green and records it. Removing it re-runs the gate.

## Security model

- `review.yml` uses `pull_request_target`, so the workflow and `team/team.py` run from `main`. PR code is never checked out or executed in that job; a PR cannot edit its own gate.
- Reviewers read PRs as data: `team.py context` shows the diff, and the briefs forbid checking out or running the branch.
- `.gemini/settings.json` limits the Gemini agents' tools to reading files, writing their review file, `team.py`, read-only `git` and `gh` view commands. That restriction is what makes `--approval-mode yolo` acceptable for reviewers: a prompt injection in a diff has very little to work with. Verify the tool names against your Gemini CLI version.
- Tim's `--allowedTools` list in `agents.json` keeps him to git, gh, make and file edits. `.claude/settings.json` blocks force-push and `gh pr merge`.
- `ci.yml` runs PR code with a read-only token and no secrets.
- Changes to `.github/`, `team/` and `scripts/` only take effect after they merge - review those PRs yourself.

## Limits to know

- **Partial blindness for every agent.** Claude Code and Gemini CLI each tell their model what it is in their own system prompt. The handbook, briefs and stripped provenance are best-effort framing on top.
- **Rulesets** are only enforced on public repos or paid plans.
- **Dependabot PRs** run `pull_request_target` with a read-only token, so no review request is posted. Review them yourself and apply `review-override`.
- **Gemini model IDs** `gemini-3.1-pro-preview` and `gemini-3.8-flash` are the best-known names, not confirmed here - check them in `gemini` before the first run.
- Everything posts as your account, so GitHub will not notify you of the agents' comments. Watch the `needs-maintainer` label instead.

## Free resources used or worth borrowing from

- GitHub Actions - free for public repos; private repos get a monthly minutes allowance on the free plan.
- CodeQL default setup, Dependabot alerts and security updates, private vulnerability reporting - enabled by the setup script.
- [amannn/action-semantic-pull-request](https://github.com/amannn/action-semantic-pull-request) - Conventional Commit PR titles.
- [Gemini CLI](https://geminicli.com/docs/) - tool allowlists and headless mode for the reviewers.
- [Google engineering practices: code review](https://google.github.io/eng-practices/review/) - basis for the reviewer briefs.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) - Barbara's reference.
- [Silo server](https://github.com/Silo-Server/silo-server) - source of the process: issue-first, one concern per PR, evidence standard, capped fix-or-rebut review loop, never weakening checks, hygiene gate for local paths.
