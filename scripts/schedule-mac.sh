#!/usr/bin/env bash
# Turn the team's schedule on or off on this Mac, with launchd.
#   scripts/schedule-mac.sh install     # one job per member, every 10 minutes
#   scripts/schedule-mac.sh status      # is each job loaded, and its last exit code
#   scripts/schedule-mac.sh uninstall   # remove the jobs
#   scripts/schedule-mac.sh install --dry-run   # print the job files, change nothing
#
# The jobs run from the scheduler's own clone in ~/.watchtower/clones/, which
# watch.sh keeps up to date with main, not from the clone you work in. macOS
# blocks background jobs from your Documents folder, and your working copy should
# not change under you. They run inside your login session, so Claude Code can use
# its keychain login. While the Mac sleeps nothing runs; launchd catches up once on
# wake. Works with the bash 3.2 that ships on macOS.
set -eu

cmd="${1:-}"
dry=0
[ "${2:-}" = "--dry-run" ] && dry=1
cd "$(git rev-parse --show-toplevel)"
home="${WATCHTOWER_HOME:-$HOME/.watchtower}"
repo="$(python3 -c 'import sys; sys.path.insert(0, "team"); import team; print(team.GH._detect())')"
slug="$(printf '%s' "$repo" | tr '/' '__' | tr -c 'A-Za-z0-9_\n-' '-')"
clone="$home/clones/$(printf '%s' "$repo" | sed 's#/#__#')/scheduler"
agents="$HOME/Library/LaunchAgents"
members="$(python3 -c 'import json; print(" ".join(m["id"] for m in json.load(open("team/config.json"))["members"] if m["role"] != "gate"))')"
domain="gui/$(id -u)"

label() { printf 'com.watchtower.%s.%s' "$slug" "$1"; }

plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$(label "$1")</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$clone/scripts/watch.sh</string><string>$1</string></array>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
    <key>WATCHTOWER_HOME</key><string>$home</string>
  </dict>
  <key>StandardOutPath</key><string>$home/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$home/logs/launchd.log</string>
</dict>
</plist>
EOF
}

case "$cmd" in
  install)
    for tool in python3 gh git openssl claude; do
      command -v "$tool" >/dev/null || { echo "! $tool is not on your PATH - install it first"; exit 1; }
    done
    if [ "$dry" -eq 1 ]; then
      for m in $members; do echo "== $agents/$(label "$m").plist"; plist "$m"; done
      exit 0
    fi
    mkdir -p "$home/logs" "$agents"
    if [ ! -d "$clone/.git" ]; then
      mkdir -p "$(dirname "$clone")"
      git clone -q "https://github.com/$repo.git" "$clone"
      echo "cloned $repo for the scheduler into $clone"
    fi
    for m in $members; do
      file="$agents/$(label "$m").plist"
      plist "$m" >"$file"
      plutil -lint -s "$file"
      launchctl bootout "$domain/$(label "$m")" 2>/dev/null || true
      launchctl bootstrap "$domain" "$file"
      echo "scheduled $m (every 10 minutes): $file"
    done
    echo "Logs: $home/logs/   Check: scripts/schedule-mac.sh status"
    ;;
  status)
    for m in $members; do
      if out="$(launchctl print "$domain/$(label "$m")" 2>/dev/null)"; then
        printf '%-16s loaded, %s\n' "$m" "$(printf '%s\n' "$out" | grep -m1 'last exit code' | sed 's/^[[:space:]]*//')"
      else
        printf '%-16s not scheduled\n' "$m"
      fi
    done
    ;;
  uninstall)
    for m in $members; do
      launchctl bootout "$domain/$(label "$m")" 2>/dev/null || true
      rm -f "$agents/$(label "$m").plist"
      echo "removed $m"
    done
    echo "The scheduler's clone and logs in $home are kept; delete them yourself if you want."
    ;;
  *)
    sed -n '2,6p' "$0"
    exit 2
    ;;
esac
