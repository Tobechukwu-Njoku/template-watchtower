#!/usr/bin/env bash
# Build, lint and test whatever stacks exist in the repo. Called by `make ci`
# locally and in CI, so both run the same gate. Add project-specific steps to the
# Makefile's `ci` target rather than to the workflow.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
ran=0

if [ -f package.json ]; then
  ran=1
  if [ -f pnpm-lock.yaml ]; then corepack enable; pnpm install --frozen-lockfile; pm=pnpm
  elif [ -f yarn.lock ]; then corepack enable; yarn install --immutable; pm=yarn
  else npm ci; pm=npm; fi
  for s in lint typecheck build test; do
    if node -e "process.exit(require('./package.json').scripts?.['$s'] ? 0 : 1)"; then $pm run "$s"; fi
  done
fi

if [ -f go.mod ]; then
  ran=1
  unformatted="$(gofmt -l .)"
  if [ -n "$unformatted" ]; then echo "gofmt needed on:"; echo "$unformatted"; exit 1; fi
  go build ./...
  go vet ./...
  go test ./...
fi

if [ -f pyproject.toml ] || ls requirements*.txt >/dev/null 2>&1; then
  ran=1
  python -m pip install --upgrade pip >/dev/null
  if [ -f pyproject.toml ]; then pip install -e ".[dev]" 2>/dev/null || pip install -e .; fi
  for r in requirements*.txt; do [ -f "$r" ] && pip install -r "$r"; done
  if command -v ruff >/dev/null; then ruff check .; fi
  if command -v pytest >/dev/null; then pytest -q; fi
fi

[ "$ran" -eq 1 ] || echo "ci: no package.json, go.mod or pyproject.toml yet - nothing to build"
