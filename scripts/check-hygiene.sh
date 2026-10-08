#!/usr/bin/env bash
# Repository hygiene gate. Fails on things that should never be committed.
#   scripts/check-hygiene.sh            # all tracked files (CI)
#   scripts/check-hygiene.sh --cached   # staged files only (pre-commit hook)
# Works with the bash 3.2 that ships on macOS.
set -eu
cd "$(git rev-parse --show-toplevel)"

if [ "${1:-}" = "--cached" ]; then
  list() { git diff --cached --name-only --diff-filter=ACMR; }
else
  list() { git ls-files; }
fi

# Patterns are assembled from pieces so this script does not flag itself.
H='/'"Users"'/[A-Za-z0-9._-]+/|/'"home"'/[A-Za-z0-9._-]+/|[A-Za-z]:\\'"Users"'\\'
S='AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|sk-ant-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|-----BEGIN [A-Z ]*PRIVATE'" KEY-----"
C='^(<{7}|={7}|>{7})( |$)'

fail=0
count=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  count=$((count + 1))
  case "$f" in scripts/check-hygiene.sh) continue ;; esac
  size=$(wc -c <"$f" | tr -d ' ')
  if [ "$size" -gt 5242880 ]; then echo "::error file=$f::file is over 5 MB - keep it out of the repo or use Git LFS"; fail=1; fi
  grep -Iq . "$f" 2>/dev/null || continue # skip binary and empty files
  if grep -qE "$S" "$f"; then echo "::error file=$f::looks like a secret or private key"; fail=1; fi
  if grep -qE "$C" "$f"; then echo "::error file=$f::merge conflict marker"; fail=1; fi
  case "$f" in *.md|*.txt|docs/*)
    if grep -qE "$H" "$f"; then echo "::error file=$f::local absolute path - use repository-relative paths"; fail=1; fi ;;
  esac
done <<EOF
$(list)
EOF

[ "$fail" -eq 0 ] && echo "hygiene: ok ($count files)"
exit "$fail"
