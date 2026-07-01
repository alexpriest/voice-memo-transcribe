# voice-memo-transcribe

Auto-transcribes new Apple Voice Memos into Alex's Obsidian daily notes.

A new memo lands → it's detected, transcribed on-device (mlx-whisper), lightly cleaned and
wikilinked by Claude, and written into the correct daily note under a collapsible toggle with
the audio embedded for inline playback. Kit texts Alex only when a word is garbled or a link
is a guess.

It also renames each memo in the Voice Memos app itself (so the app shows a meaningful,
dated title that syncs to iPhone) — see the rename runner below.

See [SPEC.md](./SPEC.md) for the full design. Runs on the Mac Mini via launchd.

## Status

Live since 2026-06-29 (Mac Mini), fully autonomous since 2026-06-30. Two launchd agents:
- `com.alexpriest.voice-memo-transcribe` — watches the Recordings dir + 06:30 catch-up; transcribes.
- `com.alexpriest.voice-memo-rename` — every 180s, renames the app backlog when it's safe (below).

History seeded/ignored; only new memos are processed.

## App rename runner

Apple exposes no rename API and a raw DB write doesn't sync, so the runner drives the real
**Voice Memos → File → Rename** UI via AppleScript/Accessibility (which *does* sync via CloudKit).
It only touches the UI when it's safe: the screen must be **unlocked** and the user **idle ≥ 120s**
(Quartz `CGSSessionScreenIsLocked` + `CGEventSourceSecondsSinceLastEventType`). It `caffeinate`s
the display during a pass and stops the instant input resumes. Note: it cannot run while iPhone
Mirroring is active (that locks the Mac). Backlog is tracked in the ledger (`app_renamed`).
Run on demand (bypass the idle gate): `python process_voice_memos.py --rename-queue --force`.

## Permissions (required — two grants, same binary)

Both target `.venv/bin/python3.11` (a standalone `--copies` binary, so grants are scoped to this
tool). System Settings → Privacy & Security:
- **Full Disk Access** — to READ the TCC-protected Voice Memos container. Without it the job dies
  with `PermissionError` on `CloudRecordings.db`.
- **Accessibility** — to DRIVE the Voice Memos rename UI. Without it the rename runner can't
  control the app.

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
