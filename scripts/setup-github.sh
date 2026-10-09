#!/usr/bin/env bash
# One-time GitHub setup for this repo. Idempotent - safe to re-run.
#   scripts/setup-github.sh <repo-name> --public|--private
#
# Requires: gh (logged in with repo + workflow scopes), git, python3.
# Note: rulesets (the merge gate) are only enforced on public repos, or on
# private repos with GitHub Pro/Team. On a free private repo everything else
# works, but the gate is advisory.
#
# Merging to main needs the checks below, each reported by GitHub Actions itself
# (integration 15368), plus one approval from a code owner. The repository admin
# role may bypass the pull request rules - that is how you merge your own PRs,
# which GitHub will not let you approve - but nobody can push to main directly.
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
chmod +x scripts/*.sh .githooks/* team/*.py
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
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request" }
  ],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_linear_history" },
    { "type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": false,
        "required_review_thread_resolution": true } },
    { "type": "required_status_checks", "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "review/gate", "integration_id": 15368 },
          { "context": "PR title", "integration_id": 15368 },
          { "context": "Hygiene", "integration_id": 15368 },
          { "context": "Build and test", "integration_id": 15368 },
          { "context": "Team self-test", "integration_id": 15368 } ] } }
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

say "Labels for the team"
gh label create ready --repo "$repo" --color 0E8A16 --description "Agreed and ready for implementation" --force >/dev/null
gh label create needs-maintainer --repo "$repo" --color FBCA04 --description "Waiting on a maintainer decision" --force >/dev/null

say "Team Apps (one per identity - see docs/maintainers/pipeline.md)"
# Asking each App for a token proves its key is on this machine and it is installed here.
python3 -c 'import json
c = json.load(open("team/config.json"))
for key, a in c["apps"].items():
    print(a["slug"], next(m["id"] for m in c["members"] if m.get("app") == key))' |
while read -r slug member; do
  if err="$(python3 team/team.py token --as "$member" 2>&1 >/dev/null)"; then
    echo "   $slug: installed and working"
  else
    warn "$slug: $err"
  fi
done

say "Check in Settings > Rules > Rulesets > main that 'Repository admin' is listed under bypass."
say "Done: https://github.com/$repo"
