#!/usr/bin/env bash
# One team member's scheduled check. Run every 10 minutes per member, by
# scripts/schedule-mac.sh (launchd) or your own scheduler:
#
#   scripts/watch.sh barbara-gordon
#
# `team.py tick` applies the schedule (hourly when the repo is quiet, every 10
# minutes while it is active), then `team.py run` handles the member's inbox:
# reviewers through Antigravity, Tim through Claude Code in his own clone. An idle
# check costs a few GitHub API calls and no model time.
#
# Everything runs as the member's own GitHub App: a one-hour token from
# `team.py token` goes in GH_TOKEN, so the team never uses your personal login.
# Logs: $WATCHTOWER_HOME/logs/ (default ~/.watchtower/logs/).
#
# Works with the bash 3.2 that ships on macOS. Everything is inside main(), which
# bash reads whole before running it, so the `git pull` below can safely replace
# this file mid-run.
set -u

main() {
  id="${1:?usage: watch.sh <member-id>}"
  root="$(cd "$(dirname "$0")/.." && pwd)"
  home="${WATCHTOWER_HOME:-$HOME/.watchtower}"
  mkdir -p "$home/locks" "$home/logs"
  name="$(basename "$(dirname "$root")")-$(basename "$root")"
  log="$home/logs/$name-$id.log"
  if [ -f "$log" ] && [ "$(wc -c <"$log" | tr -d ' ')" -gt 1048576 ]; then
    mv "$log" "$log.1"
  fi

  # One run per member at a time. A lock older than 3 hours is from a crashed run.
  lock="$home/locks/$name-$id"
  find "$lock" -maxdepth 0 -mmin +180 -exec rmdir {} \; 2>/dev/null
  mkdir "$lock" 2>/dev/null || return 0
  trap 'rmdir "$lock"' EXIT

  cd "$root" || return 1
  # The scheduler's own clone keeps itself current. A clone you work in is left alone.
  case "$root" in
    "$home"/clones/*) git pull -q --ff-only >>"$log" 2>&1 || echo "git pull failed in $root" >>"$log" ;;
  esac

  GH_TOKEN="$(python3 team/team.py token --as "$id" 2>>"$log")" || return 0
  export GH_TOKEN
  python3 team/team.py tick --as "$id" >/dev/null 2>>"$log" || return 0

  echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $id" >>"$log"
  WATCHTOWER_AGENT="$id" python3 team/team.py run --as "$id" >>"$log" 2>&1
}

main "$@"; exit $?
