#!/bin/bash
# Install voice-memo-transcribe: deps, model, vault gitignore, launchd agent.
# Idempotent — safe to re-run. Mac Mini only (avoid double-processing).
set -euo pipefail

TOOL_DIR="/Users/alex/Code/tools/voice-memo-transcribe"
VAULT="/Users/alex/Obsidian/alexpriest"
PLIST_SRC="$TOOL_DIR/com.alexpriest.voice-memo-transcribe.plist"
PLIST_DST="/Users/alex/Library/LaunchAgents/com.alexpriest.voice-memo-transcribe.plist"
GITIGNORE="$VAULT/.gitignore"
AUDIO_IGNORE="System/Voice Memos/Audio/"

cd "$TOOL_DIR"

echo "==> venv + deps"
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> pre-downloading whisper model"
.venv/bin/python -c "from huggingface_hub import snapshot_download; \
snapshot_download('mlx-community/whisper-large-v3-turbo')"

echo "==> vault .gitignore (keep audio out of git + website sync)"
if ! grep -qxF "$AUDIO_IGNORE" "$GITIGNORE" 2>/dev/null; then
    printf '\n# Voice memo audio — Mac-local, never committed or web-synced\n%s\n' "$AUDIO_IGNORE" >> "$GITIGNORE"
    echo "   added: $AUDIO_IGNORE"
else
    echo "   already present"
fi

echo "==> seeding ledger (ignore all existing memos)"
.venv/bin/python process_voice_memos.py --seed-ledger

echo "==> launchd agent"
cp "$PLIST_SRC" "$PLIST_DST"
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "   loaded $PLIST_DST"

echo "==> done. Logs: ~/Library/Logs/voice-memo-transcribe.log"
echo "    Backfill today on demand: .venv/bin/python process_voice_memos.py --backfill-since $(date +%Y-%m-%d)"
