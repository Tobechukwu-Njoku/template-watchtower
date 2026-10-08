.PHONY: setup ci hygiene review-local review-again selftest

PANEL := python3 .github/review/panel.py
BASE  ?= origin/main

## One-time: use the repo's git hooks.
setup:
	git config core.hooksPath .githooks
	@echo "hooks enabled"

## The same gate CI runs.
ci: hygiene
	scripts/ci.sh

hygiene:
	scripts/check-hygiene.sh

## Pre-PR review of this branch by the team, against $(BASE). Put your draft PR description in .pr-body.md.
review-local:
	@mkdir -p .review
	$(PANEL) review --reviewer all --local --base $(BASE) --out .review $(if $(wildcard .pr-body.md),--body-file .pr-body.md) | tee .review/round.md

## Second round, after fixes. Feeds the last round back so settled findings drop out.
review-again:
	@rm -rf .review/previous && mkdir -p .review/previous && cp .review/*.md .review/previous/
	$(PANEL) review --reviewer all --local --base $(BASE) --out .review --previous .review/previous $(if $(wildcard .pr-body.md),--body-file .pr-body.md) | tee .review/round.md

selftest:
	python3 .github/review/selftest.py
