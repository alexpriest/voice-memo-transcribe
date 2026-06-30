#!/bin/bash
# launchd entrypoint for voice-memo-transcribe.
# Fired by ~/Library/LaunchAgents/com.alexpriest.voice-memo-transcribe.plist
# on changes to the Voice Memos Recordings dir + a daily catch-up.
set -uo pipefail

TOOL_DIR="/Users/alex/Code/tools/voice-memo-transcribe"
LOG="/Users/alex/Library/Logs/voice-memo-transcribe.log"
LOCK="/tmp/voice-memo-transcribe.lock"
VENV_PY="$TOOL_DIR/.venv/bin/python"

# Single instance — WatchPaths can fire many times mid-recording/sync.
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Let recording / iCloud writes settle before reading the DB + files.
sleep 25

cd /Users/alex/Obsidian/alexpriest || { echo "FATAL: cannot cd to vault" >> "$LOG"; exit 1; }

echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') — voice-memo run starting =====" >> "$LOG"
"$VENV_PY" "$TOOL_DIR/process_voice_memos.py" "$@" >> "$LOG" 2>&1
EXIT=$?
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') — finished (exit $EXIT) =====" >> "$LOG"
exit $EXIT
