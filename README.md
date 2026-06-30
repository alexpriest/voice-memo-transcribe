# voice-memo-transcribe

Auto-transcribes new Apple Voice Memos into Alex's Obsidian daily notes.

A new memo lands → it's detected, transcribed on-device (mlx-whisper), lightly cleaned and
wikilinked by Claude, and written into the correct daily note under a collapsible toggle with
the audio embedded for inline playback. Kit texts Alex only when a word is garbled or a link
is a guess.

See [SPEC.md](./SPEC.md) for the full design. Runs on the Mac Mini via launchd.

## Status

Live since 2026-06-29 (Mac Mini), fully autonomous since 2026-06-30. launchd agent
`com.alexpriest.voice-memo-transcribe` watches the Recordings dir + a 06:30 daily catch-up.
History seeded/ignored; only new memos are processed.

## Full Disk Access (required)

The Voice Memos container is TCC-protected, so the background job needs **Full Disk Access**
granted to its interpreter: `.venv/bin/python3.11` (a standalone `--copies` binary, so the
grant is scoped to this tool). Without it the job dies with `PermissionError` on
`CloudRecordings.db`. Grant via System Settings → Privacy & Security → Full Disk Access → +.
The venv is built with `--copies` specifically so this binary is independently grantable.

## Install (after build)

```sh
./install.sh
```

Creates the venv, installs mlx-whisper, pre-downloads the model, loads the launchd agent, and
adds the audio folder to the vault `.gitignore`.

## Ops

- Logs: `~/Library/Logs/voice-memo-transcribe.log`
- Ledger: `state/processed.json`
- One-time backfill: `python process_voice_memos.py --backfill-since YYYY-MM-DD`
