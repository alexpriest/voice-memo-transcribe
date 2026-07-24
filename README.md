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

## App rename runner (Core Data write — the 2026-07-23 rewrite)

Apple exposes no rename API, so this used to drive the **File → Rename** UI via
AppleScript/Accessibility. That path was structurally fragile — the title text field only
exists for the *selected* row, Voice Memos virtualizes its list (off-screen rows aren't in the
AX tree), and the tree answered differently run to run. It only ever worked for brand-new memos
at the top of the list; backfilling five years of history was hopeless.

**It now writes the title THROUGH Core Data** (`bin/vmrename.swift` → `bin/vmrename`), and that
syncs to the iPhone. Verified 2026-07-23: a Core Data write emits a persistent-history
transaction, `voicememod` exports it to CloudKit (`ANSCKRECORDMETADATA.ZNEEDSUPLOAD → 0`), and it
lands on the phone. A **raw SQL UPDATE does not** — it writes no history row, so the mirroring
delegate never sees it, and the CKRecord keeps the old title and can later overwrite you.

How it works each pass (`--rename-queue`, no flags, headless — **no UI, no stolen focus**, so all
the old unlock/idle/on-call gating is gone):
1. `extract_model()` dumps Voice Memos' Core Data model out of `Z_MODELCACHE` **fresh every run**.
   Never cache it: a model that drifts from the store would trigger a migration that silently
   drops columns from every recording.
2. Build a `uid → "YYYY-MM-DD — title"` batch for every ledger entry with a title and
   `app_renamed == False` (skipping rows already correct, or gone to Recently Deleted).
3. `bin/vmrename <model.mom> <db> --batch <tsv>` opens the store through Core Data with a **hard
   compatibility gate** (`isConfiguration(...compatibleWithStoreMetadata:)` — abort, never
   migrate), sets `encryptedTitle` + `customLabelForSorting` (leaves `customLabel`, an ISO
   timestamp, alone), and saves **one transaction per memo**. Author is `voice-memo-rename` — not
   the `NSCloudKitMirroringDelegate.*` prefix, which is the only author class the history
   analyzer skips for export. Default `NSErrorMergePolicy` is kept so a concurrent Voice Memos
   write fails the save loudly instead of clobbering.
4. Mark `app_renamed` only for uids that came back `OK` *and* whose live DB title now matches.

New memos are picked up the same way, so a freshly-transcribed memo is renamed and synced within
one runner cycle. Rebuild after editing the Swift: `swiftc -O bin/vmrename.swift -o bin/vmrename`.

**Backup before the first real write:** `state/db-backup-<ts>/` holds `CloudRecordings.db` +
`-wal` + `-shm`. Restore is a plain file copy back into the Recordings container.

## Call gate (never runs while Alex is on a video call)

The **transcriber** skips itself whenever the camera or microphone is in use — Alex is on a call
or recording — so it doesn't hog the Neural Engine and make the call choppy. (The rename runner
no longer needs this: a Core Data write touches no UI and no compute-heavy path.)

Detection is a tiny compiled Swift helper, `bin/callguard`, reading CoreMediaIO's
`kCMIODevicePropertyDeviceIsRunningSomewhere` (camera) + CoreAudio's equivalent (mic). It reads
hardware *state* only — no capture session, so it needs **no** camera/mic permission and never
trips the privacy indicator. Virtual devices (Teams/Zoom audio) correctly read idle, so the gate
never gets stuck permanently "on."

It **fails open**: if the helper is missing or errors, the transcriber proceeds rather than halt —
a broken probe must never silently stop transcription. Deferred memos are retried by the next
memo event or the 06:30 catch-up. Rebuild the helper:
`swiftc -O -framework CoreMediaIO -framework CoreAudio bin/callguard.swift -o bin/callguard`.

## Permissions (required)

`.venv/bin/python3.11` (a standalone `--copies` binary, so the grant is scoped to this tool) needs
**Full Disk Access** — System Settings → Privacy & Security. It reads the TCC-protected Voice
Memos container *and* writes `CloudRecordings.db` through Core Data; without it the job dies with
`PermissionError` on `CloudRecordings.db`. (Accessibility is no longer required — the old UI
automation that needed it is gone.)

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
