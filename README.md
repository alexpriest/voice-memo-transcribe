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

## The Apple layer was silently load-bearing (fixed 2026-08-14, ANT-461)

**This is the first failure that was not in our code, and it is the worst one, because
everything downstream reported healthy the entire time.**

On 2026-08-14 Alex asked about a memo he had recorded on 8/13. It had never been
transcribed — and neither had one from 8/11. Both were sitting fully uploaded in iCloud.
Neither had ever reached this Mac.

**Root cause: nothing on this machine was pulling.** `voicememod` is the macOS daemon that
syncs Voice Memos from CloudKit into `CloudRecordings.db`, and
`/System/Library/LaunchAgents/com.apple.voicememod.plist` has **no `RunAtLoad` and no
`KeepAlive`**. It sets `EnablePressuredExit` and `EnableTransactions`, and its only launch
triggers are its MachServices: `com.apple.aps.voicememod` (the APNs push) and
`com.apple.voicememod.datastore.Cloud` / `.xpc` (the Voice Memos app). Alex never opens
Voice Memos on the Mini. So the whole pipeline hung off a single push arriving and being
acted on — **with no retry of any kind.** Miss one and every later memo is stranded
indefinitely.

⚠️ **"voicememod is not running" is NOT itself the bug** — idle exit is by design. The bug
is that nothing ever wakes it again. Do not "fix" this by trying to keep it alive.

**Why five days passed unnoticed.** `query_memos()` read the DB perfectly; it was the *DB*
that was frozen. So the poll logged `33 eligible memos in DB / nothing new to process`
**235 consecutive times**, which is byte-identical to a quiet week. The FDA silent-failure
guard never fired because nothing failed. The only physical tell was
`CloudRecordings.db-wal` frozen at 2026-08-08 20:41 — the timestamp of *our own renamer's*
write, i.e. the last thing to touch that database was us, not Apple.

**The fix: stop trusting the push, pull on every poll.** `kick_icloud_sync()` runs
`launchctl kickstart gui/<uid>/com.apple.voicememod` immediately before the existing
`SETTLE_SECONDS` sleep, so the CloudKit fetch lands inside a window we were already paying
for. Measured: two stranded memos finished downloading ~26s after the kickstart, which is
why `SETTLE_SECONDS` went 25 → 35.

- **Plain `kickstart`, never `-k`.** Verified idempotent on a running job (exit 0, PID
  unchanged). `-k` would kill a possibly mid-download daemon to cover a failure mode there
  is no evidence of.
- **Best effort, never fatal.** This runs before every poll, so if it could raise it would
  turn a partial outage into a total one. That invariant is the load-bearing test in
  `test_icloud_sync.py`.
- **`--rename-queue` kicks too, after its writes.** A title only reaches Alex's iPhone when
  voicememod exports our Core Data persistent-history transaction to CloudKit, so the same
  dead daemon breaks *both* halves — memos stranded coming in, titles stranded going out.
- **The poll now logs the newest memo's date, not just the count.** A count says nothing
  about whether the DB is live or frozen; a newest-date that stops advancing is obvious.
- **`voicememod was DOWN — revived it`** is logged only when we actually revived it. How
  often that line appears is the only data anyone will have if this recurs.

Verified end-to-end through launchd and the Agent Tools wrapper, not from a shell: killed
voicememod, ran the job, watched it log the revival and bring the daemon back.

## Reliability — three failure modes, all fixed 2026-07-30

A single memo (2026-07-30) tripped all three in sequence. Worth understanding before touching
this code, because two of them fail *silently*.

**1. The sync race.** The launchd job is `WatchPaths`-triggered on the Recordings directory, so
it fires the instant a memo lands — which is always inside the `MIN_MTIME_AGE_S` (30s) window
that `is_file_ready()` uses to detect a still-syncing file. The trigger and the guard were in
direct opposition: wake up, see a too-fresh file, log `skip`, exit. Nothing rescheduled it, so
the memo waited for an unrelated directory change or the 06:30 calendar run. 2026-07-25 hit this
and recovered by luck; 2026-07-30 didn't.
→ `wait_until_ready()` now blocks and polls rather than skipping. A `StartInterval` of 1800s was
added to the plist as a backstop for any *other* missed trigger (asleep machine, crash, job
unloaded). Both triggers coexist — verify with `launchctl print` showing `watching = 1` **and**
`run interval = 1800`.

**2. No retry on the Craft write.** `craft_write._req()` had none, so one transient gateway error
killed a write that had already cost a full transcribe + enrich. Craft's edge does return
intermittent 502s — observed repeatedly.
→ Now retries `429/502/503/504` and network errors with backoff. **`500` is deliberately NOT
retried** — unlike a gateway error it may have partially applied, and replaying a non-idempotent
`POST /blocks` would duplicate.

**3. `status: "error"` was terminal.** `select_memos()` skipped *any* ledger entry, so a memo that
errored could never be picked up again by any trigger. Permanent, not delayed.
→ Errors are now retried up to `MAX_ERROR_RETRIES`, with an `attempts` counter.

⚠️ **The trap in fixing #3:** retrying a *partially* written memo would append a duplicate toggle,
because `_find_existing_group()` can only match on a toggle id and the error entry didn't record
one. `write_voice_memo` now attaches `toggle_id` to the exception and the error ledger entry
persists it, so the retry *replaces* the partial group. If you add a new failure path between
toggle creation and completion, it must preserve the toggle id the same way.

## Notifications — one text per memo, sent after the write (fixed 2026-08-03)

A fourth failure mode, worth its own section because the fix constrains where the send may live.

The enrich prompt used to instruct the model to **send the SMS itself**. That made the
notification a side effect of a step that runs *before* the write and is re-run on every retry.
When the 2026-08-02 memo's Craft write 404'd, the retry loop texted Alex **four times** — each
with a different invented title, each flagging the same garbled phrase.

→ `enrich()` now only *returns* `NOTIFY_TEXT`; the prompt tells the model unconditionally not to
send and not to use tools. `main()` sends via `_send_sms()` **after** `place_in_craft` and
`append_activity` both succeed, and records `notified` / `notified_at` in the ledger. Every path
that rewrites a ledger entry — including both error paths — carries `notified` forward, so a memo
Alex has already been texted about stays quiet across runs, retries, and forced `--uids`
reprocesses. `--no-notify` gates the send in `main()` and still reports what it would have sent.

**If you add a new notification, put it after the write and give it a ledger flag.** A send that
happens before the durable write will be re-sent by the retry loop.

**The bar for texting at all is high** (Alex's call, 2026-08-03, after the model flagged a false
start that changed nothing). `should_notify` is false by default; step 5 of the prompt flips it
true only for a *load-bearing* unclear span — one that changes what he meant, or garbles a number,
amount, date, name, or commitment — or a guessed link to someone outside the inner circle. False
starts, filler, self-corrections and anything obvious from context stay silent: the transcript
already carries its `[unclear: ...]` markers, so a text is only for what he'd want to know
**without** opening the note.

Related: `craft_write._blocks()` treats a `404 NOT_FOUND_ERROR / dailyNote` as an empty note
rather than raising — `POST /blocks` with a `date` position auto-creates the daily note, so a
missing note was never a real failure. That 404 is what drove the retry loop in the first place.

## Ops

- Logs: `~/Library/Logs/voice-memo-transcribe.log`
- Ledger: `state/processed.json`
- One-time backfill: `python process_voice_memos.py --backfill-since YYYY-MM-DD`
- Force a specific memo (ignores ledger status): `python process_voice_memos.py --uids <UID>`
