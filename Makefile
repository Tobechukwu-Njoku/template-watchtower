.PHONY: setup ci hygiene inbox selftest apps

## One-time: use the repo's git hooks.
setup:
	git config core.hooksPath .githooks
	@echo "hooks enabled"

## The same gate CI runs.
ci: hygiene
	scripts/ci.sh

hygiene:
	scripts/check-hygiene.sh

## What needs a team member now: make inbox AS=tim-drake
inbox:
	@python3 team/team.py inbox --as $(AS)

selftest:
	python3 team/selftest.py

## Once per GitHub account: create the team's GitHub Apps (opens your browser).
apps:
	scripts/create-apps.py
