# Watchtower - maintainer notes

## New project from this template

1. On GitHub: **Use this template -> Create a new repository**. Choose Public if you want the merge gate enforced on the free plan.
2. Clone it, then run `scripts/setup-github.sh <repo-name> --public` (merge settings, label, ruleset, security features).
3. `gh secret set GEMINI_API_KEY --repo <owner>/<repo-name>`
4. Update `.github/CODEOWNERS` if the owner differs, fill in "The project" in `AGENTS.md`, and rewrite `README.md`.
5. Check models: `GEMINI_API_KEY=... python3 .github/review/panel.py models`.


This file is for you, not the team. `.claude/settings.json` blocks the implementer from reading it, `.github/review/` and `review.yml`, so the framing stays intact.

## The idea

Every participant is an AI agent framed as a human colleague with one lens. Nothing they see says they are AIs, or that the code under review was machine-written. Two reasons:

1. **Better reasoning.** A reviewer playing "senior engineer with a reputation" behaves differently from one playing "helpful assistant". The charters push toward concrete, evidence-backed findings and away from both flattery and over-flagging.
2. **No poisoned well.** Reviewers told "this is AI-generated code" tend to hunt for stereotypical AI faults and over-flag. Reviewers told nothing judge the diff on its merits.

Provenance is kept for you, never for them: the PR template's **Provenance** section and every `Co-Authored-By` trailer are stripped from the reviewers' view, and James Gordon's gate comment lists the trailers under a collapsed "Provenance" block.

## The loop

```
issue -> Tim Drake implements (Claude Code, AGENTS.md)
      -> make ci -> make review-local / review-again  (pre-review, max 3 rounds)
      -> PR opened
          CI:      PR title, Hygiene, Build and test, Panel self-test     (pull_request, no secrets)
          Review:  Barbara | Lucius | Bruce | Victor  -> James Gordon     (pull_request_target)
                   each posts one comment, updated in place each round
                   gate sets the review/gate commit status
      -> Tim addresses findings (/address-review), pushes -> next round
      -> all required checks green -> you merge (squash)
```

## Team and model assignments

| Name | Role | Primary model | Fallbacks | Runs in |
|---|---|---|---|---|
| Tim Drake | Implementer | Claude Opus 5.5 (Claude Code, Ultra) | - | Your machine |
| Barbara Gordon | Security and data protection | Gemini 3.1 Pro, High thinking | Gemini 3.8 Flash High, then GitHub Models gpt-4.1 | GitHub Actions |
| Lucius Fox | Architecture and design | Gemini 3.1 Pro, High thinking | Gemini 3.8 Flash High, then GitHub Models gpt-4.1 | GitHub Actions |
| Bruce Wayne | Adversarial review | Gemini 3.1 Pro, High thinking | Gemini 3.8 Flash High, then GitHub Models gpt-4.1 | GitHub Actions |
| Victor Stone | Verification and tests | Gemini 3.8 Flash, High thinking | Gemini 3.1 Pro High, then GitHub Models gpt-4.1-mini | GitHub Actions |
| James Gordon | Merge gate | None - deterministic script | - | GitHub Actions |

Why this split:

- **Claude writes, Gemini reviews.** No model judges output from its own family, which removes self-preference bias from review.
- **Pro on judgement-heavy lenses** (security, design, breaking things). **Flash on Victor**, whose job is more mechanical: does the evidence match the diff, are there tests.
- **The gate has no model**, so the merge decision is reproducible and cannot be talked round.
- **GitHub Models is the last fallback** - free, needs no key, but only about 8k input tokens, so it is skipped automatically for chunks that will not fit.

### Using this as a template

Models live in two places, so a new project changes at most two lines of config per tier:

- **Reviewers:** `tiers` in `.github/review/config.json`. Each reviewer names a tier (`deep`, `fast`, `free`); edit the tier, not the reviewer. To give one reviewer its own chain, replace its `"tier"` with a `"models"` list.
- **Implementer:** `"model"` in `.claude/settings.json`. Pick the effort level in Claude Code itself.

`reasoning_effort` (`low`/`medium`/`high`) maps to Gemini's thinking level. The Gemini IDs `gemini-3.1-pro-preview` and `gemini-3.8-flash` are the best-known names but not confirmed from here - run `GEMINI_API_KEY=... python3 .github/review/panel.py models` once; it prints the assignments and marks any ID the provider does not recognise as `NOT FOUND`.

Required secret: `gh secret set GEMINI_API_KEY`. Without it every reviewer drops to the free GitHub Models tier.

## Gate rules

- Blocks on any `blocker` or `major` finding, a reviewer that errored, or a verdict for an older commit.
- The verdict is computed from the findings, so a reviewer cannot say "approve" while listing a blocker.
- Findings that cite files not in the diff are dropped as phantoms.
- A PR too large for the review budget gets an automatic `major` finding - split it.
- After 3 rounds with blockers still standing, the gate says "needs a human decision".
- **Your override:** add the `review-override` label. The gate goes green and records it. Removing the label triggers a full re-review.

## Security model

- `review.yml` uses `pull_request_target`, so the workflow and panel scripts always run from `main`. The PR's code is never checked out or executed; the diff is fetched through the API as data. A PR cannot edit its own reviewer.
- Reviewers can only post a comment. The diff is treated as untrusted; the charters tell reviewers to report injected instructions as a finding.
- `ci.yml` runs PR code with a read-only token and no secrets.
- Changes to `.github/` and `scripts/` only take effect after they merge - review those PRs yourself.

## Limits to know

- **The implementer is only partly blind.** Claude Code's own system prompt tells it what it is; `AGENTS.md` and the deny rules are best-effort framing on top. The CI reviewers are fully blind because `panel.py` owns their whole prompt.
- **GitHub Models free tier** (the last fallback, per GitHub's docs): 10-15 req/min, 50-150/day, 8k input tokens per request.
- **Rulesets** are only enforced on public repos or paid plans. On a free private repo the gate is advisory.
- **Dependabot PRs** get a read-only token under `pull_request_target`, so the panel cannot comment. Review those yourself and apply `review-override`.
- The scripts were tested offline (`selftest.py`); the first live run against a real PR is the end-to-end test.

## Setup

```sh
scripts/setup-github.sh <repo-name> --public     # or --private
gh secret set GEMINI_API_KEY                     # required for the Gemini reviewers
```

Then fill in the "The project" section of `AGENTS.md` and the README.

Open a first PR to exercise the loop, e.g. ask Tim for a small `feat:` and watch the four comments and the gate arrive.

## Free resources used or worth borrowing from

- [GitHub Models](https://docs.github.com/en/github-models) - free inference with the workflow's `GITHUB_TOKEN` (`models: read`). Last-resort fallback.
- [Gemini API](https://ai.google.dev/gemini-api/docs/openai) - OpenAI-compatible endpoint; primary reviewer provider.
- [OpenRouter](https://openrouter.ai/models?q=free) - models tagged `:free`. Optional fallback.
- GitHub Actions - free for public repos; private repos get a monthly minutes allowance on the free plan.
- CodeQL default setup, Dependabot alerts and security updates, private vulnerability reporting - enabled by the setup script.
- [amannn/action-semantic-pull-request](https://github.com/amannn/action-semantic-pull-request) - Conventional Commit PR titles.
- [Google engineering practices: code review](https://google.github.io/eng-practices/review/) - basis for the reviewer charters.
- [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) - Barbara's reference.
- [Silo server](https://github.com/Silo-Server/silo-server) - source of the process: issue-first, one concern per PR, evidence standard, pre-PR adversarial review with a capped fix-or-rebut loop, never weakening checks, hygiene gate for local paths.
