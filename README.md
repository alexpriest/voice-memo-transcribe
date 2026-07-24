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

History seeded/ignored; only new memos are processed. Source audio is matched by container:
both `.m4a` and `.qta` (QuickTime-audio; Voice Memos uses it for some recordings). The four
pre-seed `.qta` memos were seeded-as-ignored on 2026-07-08 when `.qta` support was added, so
the filter only affects memos going forward.

## App rename runner

Apple exposes no rename API and a raw DB write doesn't sync, so the runner drives the real
**Voice Memos → File → Rename** UI via AppleScript/Accessibility (which *does* sync via CloudKit).
It only touches the UI when it's safe: the screen must be **unlocked** and the user **idle ≥ 120s**
(Quartz `CGSSessionScreenIsLocked` + `CGEventSourceSecondsSinceLastEventType`). It `caffeinate`s
the display during a pass and stops the instant input resumes. Note: it cannot run while iPhone
Mirroring is active (that locks the Mac). Backlog is tracked in the ledger (`app_renamed`).
Run on demand (bypass the idle gate): `python process_voice_memos.py --rename-queue --force`.

**Backlog nudge.** On a headless/mostly-locked Mini the safe window can go days without opening,
so a queued memo could sit unrenamed unseen. When a pass is gated (locked / on a call / user
active) *and* memos are waiting, the runner texts Alex — **once per new memo** (fires only when
the pending set grows, so a stuck item nudges once and never nags). Dedupe state lives in
`state/rename_notify.json`. Clear that file to force a re-nudge.

## Call gate (never runs while Alex is on a video call)

Both runners skip themselves whenever the camera or microphone is in use — i.e. Alex is on a
call or recording. This keeps the rename runner from yanking Voice Memos to the foreground
mid-call (its idle gate otherwise *invites* it to fire while he's listening), and keeps the
transcriber from hogging the Neural Engine and making the call choppy.

Detection is a tiny compiled Swift helper, `bin/callguard`, reading CoreMediaIO's
`kCMIODevicePropertyDeviceIsRunningSomewhere` (camera) + CoreAudio's equivalent (mic). It reads
hardware *state* only — no capture session, so it needs **no** camera/mic permission and never
trips the privacy indicator. Virtual devices (Teams/Zoom audio) correctly read idle, so the gate
never gets stuck permanently "on."

It **fails open**: if the helper is missing or errors, the runners proceed rather than halt — a
broken probe must never silently stop transcription. `--force` (manual rename drain) bypasses the
gate along with the idle/lock gates. Deferred memos are retried by the next memo event or the
06:30 catch-up. Rebuild the helper: `swiftc -O -framework CoreMediaIO -framework CoreAudio bin/callguard.swift -o bin/callguard`.

## Permissions (required — two grants, same binary)

Both target `.venv/bin/python3.11` (a standalone `--copies` binary, so grants are scoped to this
tool). System Settings → Privacy & Security:
- **Full Disk Access** — to READ the TCC-protected Voice Memos container. Without it the job dies
  with `PermissionError` on `CloudRecordings.db`.
- **Accessibility** — to DRIVE the Voice Memos rename UI. Without it the rename runner can't
  control the app.

**Outage nudge.** A grant can be revoked silently (it happened 2026-07-23 — seven runs died into
the log unread while a memo sat untranscribed for over an hour). Any run-killing exception now
texts Alex naming the fix, then still dies loudly into the log. Deduped **once per outage** —
this job fires on every filesystem event, so the signature is stored in `state/fatal_notify.json`
and cleared by the next successful DB read, meaning a later outage nudges again. The guard fails
open: a broken notifier logs and gets out of the way rather than becoming a second failure mode.

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
