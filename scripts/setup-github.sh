#!/usr/bin/env bash
# One-time GitHub setup for this repo. Idempotent - safe to re-run.
#   scripts/setup-github.sh <repo-name> --public|--private
#
# Requires: gh (logged in with repo + workflow scopes), git.
# Note: rulesets (the merge gate) are only enforced on public repos, or on
# private repos with GitHub Pro/Team. On a free private repo everything else
# works, but the gate is advisory.
set -eu

name="${1:?usage: setup-github.sh <repo-name> --public|--private}"
vis="${2:?choose --public or --private}"
cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

say() { printf '\n== %s\n' "$*"; }
warn() { printf '   ! %s\n' "$*"; }

say "Git repository"
if [ ! -d .git ]; then
  git init -b main
fi
git config core.hooksPath .githooks
chmod +x scripts/*.sh .githooks/* .github/review/*.py
if ! git rev-parse HEAD >/dev/null 2>&1; then
  git add -A
  git commit -m "chore: scaffold project and review pipeline"
fi

say "GitHub repository"
owner="$(gh api user -q .login)"
repo="$owner/$name"
if ! gh repo view "$repo" >/dev/null 2>&1; then
  gh repo create "$repo" "$vis" --source . --remote origin --push
else
  git remote get-url origin >/dev/null 2>&1 || git remote add origin "https://github.com/$repo.git"
  git push -u origin main
fi

say "Merge settings: squash only, PR title as subject, auto-merge, delete merged branches"
gh api -X PATCH "repos/$repo" \
  -F allow_squash_merge=true -F allow_merge_commit=false -F allow_rebase_merge=false \
  -F allow_auto_merge=true -F delete_branch_on_merge=true \
  -f squash_merge_commit_title=PR_TITLE -f squash_merge_commit_message=PR_BODY >/dev/null

say "Actions: read-only default token, no PR approvals by workflows"
gh api -X PUT "repos/$repo/actions/permissions/workflow" \
  -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false >/dev/null

say "Labels"
gh label create review-override --repo "$repo" --color B60205 \
  --description "Maintainer decision: merge despite open review findings" --force >/dev/null

say "Ruleset on main"
ruleset=$(cat <<'JSON'
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "review/gate" },
          { "context": "PR title" },
          { "context": "Hygiene" },
          { "context": "Build and test" },
          { "context": "Panel self-test" } ] } }
  ]
}
JSON
)
existing="$(gh api "repos/$repo/rulesets" -q '.[] | select(.name=="main") | .id' 2>/dev/null || true)"
if [ -n "$existing" ]; then
  echo "$ruleset" | gh api -X PUT "repos/$repo/rulesets/$existing" --input - >/dev/null || warn "ruleset update failed (private repo on a free plan?)"
else
  echo "$ruleset" | gh api -X POST "repos/$repo/rulesets" --input - >/dev/null || warn "ruleset create failed (private repo on a free plan?)"
fi

say "Security features (free on public repos)"
gh api -X PUT "repos/$repo/vulnerability-alerts" >/dev/null 2>&1 || warn "Dependabot alerts not enabled"
gh api -X PUT "repos/$repo/automated-security-fixes" >/dev/null 2>&1 || warn "Dependabot security updates not enabled"
gh api -X PATCH "repos/$repo/code-scanning/default-setup" -f state=configured >/dev/null 2>&1 \
  || warn "CodeQL default setup not enabled (needs a public repo or GitHub Advanced Security, and a supported language)"
gh api -X PUT "repos/$repo/private-vulnerability-reporting" >/dev/null 2>&1 || true

say "Reviewer keys (GEMINI_API_KEY is required for the Gemini reviewers; the others are optional)"
for s in GEMINI_API_KEY OPENROUTER_API_KEY ANTHROPIC_API_KEY; do
  if gh secret list --repo "$repo" | grep -q "^$s"; then echo "   $s: set"; else echo "   $s: not set - gh secret set $s --repo $repo"; fi
done

say "Done: https://github.com/$repo"
