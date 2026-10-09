# Watchtower - maintainer guide

This file is for you, not the team. `.claude/settings.json` keeps Tim out of `docs/maintainers/`; the reviewers only ever see the prompt `team.py` builds for them.

## New project from this template

1. On GitHub: **Use this template -> Create a new repository**. Public if you want the merge gate enforced on the free plan.
2. Install the team's two GitHub Apps on the new repository (create them first if this is your first project - see [Team identities](#team-identities)).
3. Clone it and run `scripts/setup-github.sh <repo-name> --public`: merge settings, labels, ruleset, security features, and a check that both Apps work.
4. Update `.github/CODEOWNERS` if the owner differs, fill in "The project" in `AGENTS.md`, and rewrite `README.md`.
5. Set up the agents on your machine - see [Running the team](#running-the-team).

## The idea

Every participant is an AI agent framed as a colleague with one lens. They may know what they are; what they are never told is that the code under review was machine-written:

1. **Better reasoning.** A reviewer playing "senior engineer with a reputation" behaves differently from one playing "helpful assistant". The briefs push toward concrete, evidence-backed findings and away from both flattery and over-flagging.
2. **No poisoned well.** Reviewers told "this is AI-generated code" hunt for stereotypical AI faults. Reviewers told nothing judge the diff on its merits.

Provenance is kept for you only. `team.py` builds every reviewer prompt and strips the **Provenance** section, `Co-Authored-By` trailers, tool banners and hidden comments. James Gordon's gate comment lists the trailers in a collapsed block for you.

## Team and models

| Name | Role | Tool and model | Woken by |
|---|---|---|---|
| Tim Drake | Implementer | Claude Code - Claude Opus 5.5 (Ultra) | `ready` issues, failed gate, `To:` mentions |
| Barbara Gordon | Security and data protection | Antigravity - Gemini 3.1 Pro (High), Google account 1 | Review requests, `To:` mentions |
| Lucius Fox | Architecture and design | Antigravity - Gemini 3.1 Pro (High), Google account 1 | Review requests, `To:` mentions |
| Bruce Wayne | Adversarial review | Antigravity - Gemini 3.1 Pro (High), Google account 2 | Review requests, `To:` mentions |
| Victor Stone | Verification and tests | Antigravity - Gemini 3.8 Flash (High), Google account 2 | Review requests, `To:` mentions |
| James Gordon | Merge gate | GitHub Actions - no model | New commits, each posted review, `/review`, override label |

- **Claude writes, Gemini reviews.** No model judges output from its own family.
- **Pro on judgement-heavy lenses**, Flash on Victor, whose checks are more mechanical.
- **The gate has no model**, so the merge decision is reproducible and cannot be talked round.
- No API keys anywhere. Each agent runs under your subscription and talks to GitHub as its own GitHub App, never as you.

## How they communicate

Everything happens in issue and PR comments, through `team/team.py`:

- Each comment starts with a hidden marker (`<!-- team:comment id=bruce-wayne -->`) and ends with a sign-off. The marker says which member wrote it, and it only counts when the comment comes from that member's App (see below).
- People are addressed with a `To:` line (`To: Barbara Gordon`, `To: reviewers`, `To: maintainer`). Never @handles: those notify real GitHub users.
- Reviews carry their verdict and findings encoded in the marker, so the gate reads them without parsing prose.
- Besides the team, only you and other collaborators can address a member. Strangers' comments are ignored.

**You talk to them the same way.** Comment `To: Tim Drake` on an issue or PR, or label an issue `ready` to hand it to Tim. When an agent needs you it comments `To: maintainer` and adds the `needs-maintainer` label - filter on that label to find your queue.

## Team identities

The team posts through two GitHub Apps, so GitHub itself records who wrote what:

| App (default name) | Used by | Can | Cannot |
|---|---|---|---|
| `watchtower-tim-drake` | Tim Drake | push branches, open PRs, comment, read CI results | change workflows, merge into `main`, approve |
| `watchtower-reviewers` | Barbara, Lucius, Bruce, Victor | read code, comment and label | change code |
| GitHub Actions | James Gordon | post review requests and the gate result, set `review/gate` | - |

`team.py` accepts a review only from the reviewers' App, a review request or gate result only from GitHub Actions, and a reply as Tim only from Tim's App. Your own account can still talk to the team, but it cannot sign as one of them, and nor can the Apps sign as each other.

**Creating the Apps** (once per GitHub account, reused by every project):

```sh
make apps        # same as scripts/create-apps.py
```

For each App your browser opens GitHub's "Create GitHub App" page with the name and permissions filled in. Confirm it; the App ID and private key are saved to `~/.watchtower/apps/` (the key readable by you only), and the installation page opens. Install each App on the repositories the team works in - "Only select repositories" is fine. If a name is taken, change it on GitHub's form; the script writes the new name into `team/config.json` for you to commit.

**Tokens:** before each check, `scripts/watch.sh` asks `team.py token` for a token for that member's App. It lasts one hour and covers this repository only. Tim's Claude Code settings deny reading `~/.watchtower/`, and `team.py token` refuses to run inside an agent, so Tim cannot pick up the reviewers' key. Both keys still live on the same machine: running the team under a separate macOS user that owns `~/.watchtower/` is the stronger setup.

**Merging:** `main` needs `review/gate` and the CI checks, each reported by GitHub Actions itself, plus one approval from a code owner - you. Tim's PRs come from his App, so you can approve them. GitHub does not let you approve your own PRs; for those, the repository admin role may bypass the pull request rules when merging. Nobody can push to `main` directly.

**PRs from outside the team** get no review request: anyone can open a PR on a public repo, and the reviewers would read whatever it contains. Comment `/review` to start a round. Tim never works on a PR he did not open.

## How reviews run

Each review is one question to Gemini, asked by `team.py run` through the Antigravity connector that T3 Code installs. The reviewer is not an agent: it has no tools and no copy of the repository. It receives one prompt and sends back one review.

- **The prompt:** the shared reviewer brief, the member's brief, "The project" and "Rules" from `AGENTS.md`, the review format, then the PR: title, description and commit messages with provenance stripped, any previous review with the replies since, and the diff. `python3 team/team.py context --as barbara-gordon --pr N` prints exactly what would be sent.
- **The diff:** every line carries its line number in the new file, so findings point at the right line. Binary files are left out and named. Lock files, vendored and generated code are shown up to `diff.trim_chars` each, so a change there is still visible. A PR over `diff.max_part_chars` is reviewed in parts and the findings merged; one needing more than `diff.max_parts` parts gets a blocking "too large, split it" review.
- **The reply** must be the review JSON. If it is not, the reviewer is asked once more; if it still is not, nothing is posted and the next check tries again.
- **Questions:** a `To:` line addressed to a reviewer on a PR they have reviewed brings a fresh review that takes the reply into account. Anywhere else, they answer in a comment.
- **Nothing is kept locally.** The connector runs in an empty temporary folder, every tool request is refused, and each session's saved conversation is deleted when the run ends. Google still receives the prompts and keeps them according to its own terms and your account's activity settings.

**Google accounts.** Each reviewer is assigned an account and a model in `team/config.json` under `reviewing`. Each account is signed in once, in its own folder (`~/.watchtower/google/<account>/`), separate from your own Antigravity and T3 Code:

```sh
python3 team/team.py google-login --account google-1   # Barbara and Lucius
python3 team/team.py google-login --account google-2   # Bruce and Victor
```

A browser window opens for Google's sign-in, and the command prints which address signed in and whether the account offers the reviewers' models. Run it again any time to check an account.

**The connector** is the one T3 Code downloads when you first use its Antigravity provider. `google-login` pins the version it finds. If T3 Code later replaces it, reviews stop with a message saying so; run `google-login` again to switch, then read the next reviews closely.

## Check-in schedule

| Repository state | Each member checks every |
|---|---|
| Quiet - nothing updated in the last 60 minutes | 60 minutes |
| Active - any issue, PR, comment or push in the last 60 minutes | 10 minutes |

Drops back to hourly after 60 quiet minutes. All numbers are in `team/config.json` under `cadence`.

**Adjustment from "ask each agent to check":** having every agent wake on a timer and look for itself would spend model time on every empty check - up to 6 agents x 6 checks an hour while active. Instead `scripts/watch.sh` runs every 10 minutes from cron or launchd, applies the schedule (`team.py tick`), and only then looks for work: for a reviewer, `team.py run` handles the inbox itself; for Tim, `team.py inbox` is asked first and Claude Code launched only if there is something. An idle check costs a few GitHub API calls and no model time. The agents still see the schedule in `AGENTS.md`, so if you prefer to run them from their own scheduled tasks instead, they follow the same rules.

Expected latency: up to an hour for the first response when the repo has been quiet, 10-20 minutes per step once work is under way. A full review round usually lands within one active cycle, because the review request itself makes the repo active.

## Running the team

On the machine that will host the agents:

```sh
# 1. Clones: one for Tim to work in, one the scheduler runs from (the reviewers need no clone of their own)
git clone https://github.com/<owner>/<repo> ~/code/<repo>-tim
git clone https://github.com/<owner>/<repo> ~/code/<repo>-review

# 2. Launch config
mkdir -p ~/.watchtower
cp ~/code/<repo>-review/docs/maintainers/agents.example.json ~/.watchtower/agents.json
#    edit: replace PROJECT with <repo>, check the CLI flags against your installed Claude Code

# 3. Sign in the reviewers' Google accounts (see "How reviews run")
python3 ~/code/<repo>-review/team/team.py google-login --account google-1
python3 ~/code/<repo>-review/team/team.py google-login --account google-2

# 4. Schedule (crontab -e). Every 10 minutes; watch.sh decides whether it is time.
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh tim-drake
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh barbara-gordon
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh lucius-fox
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh bruce-wayne
*/10 * * * * ~/code/<repo>-review/scripts/watch.sh victor-stone
```

Requirements: both Apps created and installed (see [Team identities](#team-identities)), `gh`, `openssl` and `python3` on the `PATH`, `claude` logged in, T3 Code installed with its Antigravity connector downloaded, and both Google accounts signed in. Your own `gh auth login` is used only by the setup scripts; the agents use their Apps' tokens. Cron jobs get a minimal `PATH` - add a `PATH=...` line at the top of the crontab that includes `gh`, `claude`, `openssl` and `python3`. On macOS, give `cron` Full Disk Access or use a launchd agent. Logs: `~/.watchtower/logs/`.

Test one member by hand: `scripts/watch.sh barbara-gordon; tail ~/.watchtower/logs/*barbara-gordon.log`. To force a check, delete `~/.watchtower/<owner>__<repo>/<id>.json`.

## Gate rules

- `review/gate` is **pending** until all four reviewers have reviewed the current head commit, **failure** as soon as any review has a `blocker` or `major` finding, **success** when all four are in and clean.
- Reviews of an older commit do not count; every push requests a new round.
- The verdict is computed from the findings, so a reviewer cannot "approve" while listing a blocker.
- Findings citing files outside the PR are dropped as phantoms.
- After 3 rounds with blockers standing, the gate says it needs your decision.
- **Your override:** the `review-override` label turns the gate green and records it. Removing it re-runs the gate.
- **Your approval** is required as well: a green gate makes a PR ready for you, not merged.

## Security model

- `review.yml` uses `pull_request_target`, so the workflow and `team/team.py` run from `main`. PR code is never checked out or executed in that job; a PR cannot edit its own gate.
- Every member has its own GitHub identity, and the gate checks it (see [Team identities](#team-identities)). The required checks are accepted only from GitHub Actions, so nobody can mark `review/gate` green by hand.
- Actions are pinned to commit SHAs, so a moved tag cannot change what runs. Dependabot proposes updates.
- Reviewers have no tools. The connector runs in an empty folder and every tool request is refused, so instructions planted in a PR can at most mislead a review, which the other three reviewers and you still read. The brief tells them to report such instructions as a finding.
- Tim's `--allowedTools` list in `agents.json` keeps him to git, gh, make and file edits. `.claude/settings.json` blocks force-push and `gh pr merge`.
- `ci.yml` runs PR code with a read-only token and no secrets.
- Changes to `.github/`, `team/` and `scripts/` only take effect after they merge - review those PRs yourself.

## Limits to know

- **The agents know they are AIs.** Claude Code and Antigravity each say so in their own instructions. What is hidden is where the code came from.
- **Google's terms** for unattended use of a personal subscription through the connector are not something this setup can check. Google has already stopped personal sign-ins in Gemini CLI.
- **The connector is a component T3 Code downloads**, not one Google documents for this use. An update can change how it behaves; the pinned version makes that visible instead of silent.
- **Rulesets** are only enforced on public repos or paid plans.
- **Dependabot PRs** get no review request. Review them yourself, approve, and merge (or apply `review-override` first if you want the gate green).
- Agents' comments come from their Apps, so GitHub notifies you as it would for any collaborator. The `needs-maintainer` label is still the quickest way to find your queue.

## Free resources used or worth borrowing from

- GitHub Actions - free for public repos; private repos get a monthly minutes allowance on the free plan.
- CodeQL default setup, Dependabot alerts and security updates, private vulnerability reporting - enabled by the setup script.
- [amannn/action-semantic-pull-request](https://github.com/amannn/action-semantic-pull-request) - Conventional Commit PR titles.
- [Google Antigravity](https://antigravity.google) and [T3 Code](https://github.com/pingdotgg/t3code) - Gemini on your Google AI Pro subscription, reached through the connector T3 Code installs.
- [Agent Client Protocol](https://github.com/agentclientprotocol/agent-client-protocol) - how `team/acp.py` talks to that connector.
- [Google engineering practices: code review](https://google.github.io/eng-practices/review/) - basis for the reviewer briefs.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) - Barbara's reference.
- [Silo server](https://github.com/Silo-Server/silo-server) - source of the process: issue-first, one concern per PR, evidence standard, capped fix-or-rebut review loop, never weakening checks, hygiene gate for local paths.
