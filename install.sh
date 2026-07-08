#!/bin/bash
# Install voice-memo-transcribe: deps, model, vault gitignore, launchd agent.
# Idempotent — safe to re-run. Mac Mini only (avoid double-processing).
set -euo pipefail

TOOL_DIR="/Users/alex/Code/tools/voice-memo-transcribe"
VAULT="/Users/alex/Obsidian/alexpriest"
LA="/Users/alex/Library/LaunchAgents"
GITIGNORE="$VAULT/.gitignore"
AUDIO_IGNORE="System/Voice Memos/Audio/"

cd "$TOOL_DIR"

echo "==> venv + deps (--copies => standalone interpreter, grantable for Full Disk Access)"
[ -d .venv ] || python3.11 -m venv --copies .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> pre-downloading whisper model"
.venv/bin/python -c "from huggingface_hub import snapshot_download; \
snapshot_download('mlx-community/whisper-large-v3-turbo')"

echo "==> building callguard (skips both runners while Alex is on a call)"
swiftc -O -framework CoreMediaIO -framework CoreAudio \
    "$TOOL_DIR/bin/callguard.swift" -o "$TOOL_DIR/bin/callguard"

echo "==> vault .gitignore (keep audio out of git + website sync)"
if ! grep -qxF "$AUDIO_IGNORE" "$GITIGNORE" 2>/dev/null; then
    printf '\n# Voice memo audio — Mac-local, never committed or web-synced\n%s\n' "$AUDIO_IGNORE" >> "$GITIGNORE"
    echo "   added: $AUDIO_IGNORE"
else
    echo "   already present"
fi

echo "==> seeding ledger (ignore all existing memos)"
.venv/bin/python process_voice_memos.py --seed-ledger

echo "==> launchd agents (transcribe: event-driven; rename: every 180s, idle-gated)"
for name in voice-memo-transcribe voice-memo-rename; do
    cp "$TOOL_DIR/com.alexpriest.$name.plist" "$LA/com.alexpriest.$name.plist"
    launchctl unload "$LA/com.alexpriest.$name.plist" 2>/dev/null || true
    launchctl load "$LA/com.alexpriest.$name.plist"
    echo "   loaded com.alexpriest.$name"
done

PY="$TOOL_DIR/.venv/bin/python3.11"
echo ""
echo "==> ⚠️  TWO MANUAL PERMISSION GRANTS (macOS won't let a script grant these)."
echo "    Both target the same binary:  $PY"
echo "    In System Settings → Privacy & Security:"
echo "      1. Full Disk Access  → + → ⌘⇧G → paste the path → Open → toggle ON"
echo "         (lets the background job READ the protected Voice Memos folder)"
echo "      2. Accessibility     → + → ⌘⇧G → paste the path → Open → toggle ON"
echo "         (lets the rename runner DRIVE the Voice Memos rename UI)"
echo "    Then: launchctl kickstart -k gui/\$(id -u)/com.alexpriest.voice-memo-transcribe"
echo ""
echo "==> done. Logs: ~/Library/Logs/voice-memo-transcribe.log + voice-memo-rename.log"
echo "    Rename backlog now (bypass idle gate): $PY process_voice_memos.py --rename-queue --force"
