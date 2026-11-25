#!/bin/sh
# Runs the assistant's Claude Code session (inside tmux, started by the systemd user unit).
# Restarts claude if it exits; a fresh session each time keeps the context small.
export TELEGRAM_STATE_DIR="$HOME/.claude/channels/telegram-etl"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
cd "$(dirname "$0")" || exit 1
while true; do
  claude --channels plugin:telegram@claude-plugins-official --permission-mode dontAsk
  echo "claude exited ($?); restarting in 10s…"; sleep 10
done
