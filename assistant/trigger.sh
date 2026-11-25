#!/bin/sh
# Called by cron: types "/briefing <kind>" (or "/inbox") into the running assistant session.
# usage: trigger.sh morning|evening|inbox
LOG="$(cd "$(dirname "$0")" && pwd)/trigger.log"
kind="${1:-morning}"
case "$kind" in
  inbox) cmd="/inbox" ;;
  *) cmd="/briefing $kind" ;;
esac
tmux has-session -t etl-assistant 2>/dev/null || { echo "$(date '+%F %T') session not running" >> "$LOG"; exit 1; }
tmux send-keys -t etl-assistant -l "$cmd"
sleep 1
tmux send-keys -t etl-assistant Enter
echo "$(date '+%F %T') sent $cmd" >> "$LOG"
