#!/usr/bin/env bash
# Wakes one team member when, and only when, there is work for them.
# Run it every 10 minutes from cron or launchd, once per member:
#
#   */10 * * * * /path/to/clone/scripts/watch.sh barbara-gordon
#
# The schedule itself (hourly when the repo is quiet, every 10 minutes while it is
# active) is decided by `team.py tick`. An idle check costs two GitHub API calls and
# no agent time. The agent is launched only if `team.py inbox` has something.
#
# How each member is launched comes from $WATCHTOWER_HOME/agents.json
# (default ~/.watchtower/agents.json). Logs: $WATCHTOWER_HOME/logs/.
set -u

id="${1:?usage: watch.sh <member-id>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
home="${WATCHTOWER_HOME:-$HOME/.watchtower}"
agents="$home/agents.json"
mkdir -p "$home/locks" "$home/logs"
log="$home/logs/$(basename "$root")-$id.log"

# One run per member at a time. A lock older than 3 hours is from a crashed run.
lock="$home/locks/$(basename "$root")-$id"
find "$lock" -maxdepth 0 -mmin +180 -exec rmdir {} \; 2>/dev/null
mkdir "$lock" 2>/dev/null || exit 0
trap 'rmdir "$lock"' EXIT

cd "$root" || exit 1
python3 team/team.py tick --as "$id" >/dev/null 2>>"$log"
[ $? -eq 0 ] || exit 0

count="$(python3 team/team.py inbox --as "$id" --count 2>>"$log")"
[ -n "$count" ] && [ "$count" -gt 0 ] || exit 0

echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $id: $count item(s)" >>"$log"
python3 - "$agents" "$id" "$root" >>"$log" 2>&1 <<'PY'
import json, os, subprocess, sys
agents_file, member, root = sys.argv[1:]
try:
    agents = json.load(open(agents_file))
except FileNotFoundError:
    sys.exit(f"no {agents_file} - copy docs/maintainers/agents.example.json there and edit it")
spec = agents.get(member) or sys.exit(f"{member} is not configured in {agents_file}")
team = json.load(open(os.path.join(root, "team", "config.json")))
name = next(m["name"] for m in team["members"] if m["id"] == member)
prompt = (f"You are {name}. It is time for your scheduled check on this repository. "
          f"Read AGENTS.md and your brief (python3 team/team.py brief --as {member}), "
          f"then run python3 team/team.py inbox --as {member} and handle every item in it. "
          "Post only through team/team.py. Stop when your inbox is empty.")
workdir = os.path.expanduser(spec.get("workdir") or root)
# Keep the member's clone current with main so the team tool and handbook are up to date.
branch = subprocess.run(["git", "-C", workdir, "branch", "--show-current"], capture_output=True, text=True).stdout.strip()
if branch == "main":
    subprocess.run(["git", "-C", workdir, "pull", "--ff-only", "-q"])
cmd = [a.replace("{prompt}", prompt) for a in spec["command"]]
sys.exit(subprocess.run(cmd, cwd=workdir, timeout=spec.get("timeout_minutes", 60) * 60).returncode)
PY
