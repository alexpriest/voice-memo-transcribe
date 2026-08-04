# Voice Memo → Obsidian — Design Spec

**Date:** 2026-06-29
**Status:** Approved design, pre-implementation
**Owner:** Kit (Chief of Staff) for Alex

## Goal

Any new Apple Voice Memo is automatically detected, transcribed, and written into the
correct Obsidian daily note under a collapsible toggle — with the audio embedded for inline
playback, light cleanup, and `[[wikilinks]]` to known people/projects. Kit texts Alex only
when a transcription word is genuinely garbled or a wikilink is a guess.

## Confirmed decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Transcription engine | Local **mlx-whisper** (`large-v3-turbo`) | Private, offline, free; gives per-segment confidence scores that drive the "text me when unsure" feature |
| Processing depth | **Full Claude-in-the-loop** | Cleanup + wikilinks + uncertainty judgment require an LLM; matches Alex's existing agent architecture |
| Trigger | **Event-driven** (launchd `WatchPaths`) + once/day catch-up | Near-instant when the Mac is awake; catch-up covers asleep periods |
| Audio storage | **Gitignored, Mac-Mini-local** | Plays inline in Obsidian, no repo bloat, never hits the website-sync trigger; transcript is the durable cross-machine record; original survives in iCloud |
| Host | **Mac Mini only** | Avoid double-processing; ledger is machine-local |

## Non-goals (v1 / YAGNI)

- Phone-side real-time transcription (the big build — explicitly deferred)
- Speaker diarization
- Re-syncing a note if a memo is renamed/edited in Voice Memos after the fact
- Committing audio to git / cross-machine audio playback
- Backfilling years of history (one-time recent backfill available on demand only)

## Architecture

Four stages, on the Mac Mini:

```
launchd  →  run.sh  →  process_voice_memos.py
 (WatchPaths Recordings/ + daily catch-up)
        │
        ├─ 1. DETECT      read CloudRecordings.db → new finalized memos
        ├─ 2. TRANSCRIBE  mlx-whisper → text + per-segment confidence
        ├─ 3. ENRICH      claude -p (pure-ish): clean, wikilink, judge, notify
        └─ 4. PLACE       Python: copy audio, find/create daily note, insert toggle,
                          update ledger, log
```

**Division of labor:** Python owns everything deterministic (detection, transcription,
all file I/O, idempotency, **and sending the notification**). Claude owns only linguistic
judgment (cleanup, wikilinks, uncertainty) and merely *drafts* the notification text. The
LLM surface is small and well-defined.

(Claude did send the text itself until 2026-08-03. Because enrichment runs before the write
and re-runs on every retry, one memo whose Craft write kept 404-ing texted Alex four times.
See README, "Notifications".)

## Stage detail

### 1. Detect — `CloudRecordings.db`

- Source DB: `~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db`
- The DB is open by Voice Memos.app — **copy db + `-wal` + `-shm` to a temp dir, then query** (avoids lock/WAL staleness). Read-only.
- Query `ZCLOUDRECORDING`:
  ```sql
  SELECT Z_PK, ZUNIQUEID, ZENCRYPTEDTITLE, ZCUSTOMLABEL, ZDATE, ZDURATION, ZPATH
  FROM ZCLOUDRECORDING
  WHERE ZPATH LIKE '%.m4a'
    AND ZDURATION >= 3.0
  ORDER BY ZDATE ASC;
  ```
- Record time (local): `datetime(ZDATE + 978307200, 'unixepoch', 'localtime')` (Core Data epoch).
- Title: prefer `ZENCRYPTEDTITLE` (verified plaintext, holds the human name e.g. "New Recording 10", "Deer Ridge Cir 3"); fall back to `ZCUSTOMLABEL`, else "Voice Memo".
- Full audio path: `<group container>/Recordings/<ZPATH>`.
- **Filters:** `.m4a` only (skip `.qta` in-progress scraps), duration ≥ 3s (skip accidental taps),
  file exists + size > 0 + mtime settled (>30s old), `ZUNIQUEID` not already in the ledger.
- History is ignored: only memos newer than the install watermark are processed (ledger seeded
  with all existing IDs at install, OR a watermark timestamp captured at install).

### 2. Transcribe — mlx-whisper

- `mlx_whisper.transcribe(path, path_or_hf_repo="mlx-community/whisper-large-v3-turbo")`
- Returns `segments[]`, each with `text`, `avg_logprob`, `no_speech_prob`, `compression_ratio`.
- A segment is flagged **low-confidence** if `avg_logprob < -1.0` OR `no_speech_prob > 0.6`
  OR `compression_ratio > 2.4`. These flags are passed to Claude so it knows which spans are shaky.

### 3. Enrich — `claude -p`

Invocation mirrors `~/Code/tools/daily-question/run.sh`:
`claude -p --permission-mode bypassPermissions --model claude-sonnet-4-6 --max-budget-usd 0.50`,
run from the vault dir.

**Input to the prompt:**
- Raw transcript with segment boundaries + low-confidence flags
- The linkable-entity list: People/ note basenames + frontmatter `aliases`, plus project
  nouns (Cartographer, Duckbill, Firesale, Anthimeros, Syntensor, Halcie) and Projects/ folder names
- Memo metadata (title, time, duration)

**Claude's instructions:**
- Lightly clean: punctuation, capitalization, remove filler ("um", false starts), paragraph breaks.
  **Never** change meaning or invent content.
- Wrap recognized entities in `[[Exact Note Name]]` — **only** names from the supplied list (no red links).
- Mark genuinely-garbled spans `[unclear: "best guess"]`; suffix a guessed link with `?`.
- Decide `should_notify`. **False by default** (tightened 2026-08-03 — Alex's call): true only
  when the unclear span is load-bearing (it changes what he meant) or garbles a number, amount,
  date, name, or commitment, or when a link was guessed for someone outside the inner circle.
  False starts, filler, self-corrections, and anything obvious from context are never worth a
  text — the `[unclear: ...]` markers are already in the note.
- If notifying, write the **plain-text** SMS (no markdown) naming the memo + time + the specific
  flags. Claude does NOT send it and uses no tools — stage 4 sends it once the memo is written,
  and only if the ledger has not already recorded a send for that memo.

**Output:** a single minified JSON object to stdout:
```json
{"cleaned_markdown":"...","uncertainties":["..."],"notified":true,"notify_text":"..."}
```
Python extracts the first `{...}` block and parses it.

**Fallback:** if `claude -p` errors or returns no parseable JSON, Python writes the **raw**
whisper transcript under the toggle with a "(enrichment failed — raw transcript)" marker and
texts a single heads-up. A memo is never lost.

### 4. Place — Python (deterministic)

- **Target note:** `Daily/<YYYY>/<MM-Month>/<YYYY-MM-DD>.md` from the memo's record date
  (month folder e.g. `06-June`). Create year/month dirs as needed.
- **Missing note:** create with the static daily-note structure (no Templater execution):
  frontmatter (`tags: [periodic/daily]`, `title`, `daily-date`, `cssclasses: [hide-properties]`),
  `# Morning Pages` + placeholder, `---`, `# Jots`.
- **Insert:** ensure a `# Voice Memos` section exists (append at end of note if absent).
  Add the memo as a **collapsed callout**, in chronological order within the section:
  ```markdown
  > [!note]- 🎙️ {title} — {h:mm A} · {Xm Ys}
  > ![[{YYYY-MM-DD HHMM} voice-memo.m4a]]
  >
  > {cleaned_markdown with wikilinks}
  >
  > *⚠️ {uncertainties joined}*   ← only if any
  ```
  The trailing `-` on `[!note]-` = collapsed by default (the requested toggle).
- **Audio:** copy original → `System/Voice Memos/Audio/<YYYY-MM-DD HHMM> {slug}.m4a`; embed by filename.
  This folder is added to the vault `.gitignore`.
- **Ledger:** `~/Code/tools/voice-memo-transcribe/state/processed.json`, keyed by `ZUNIQUEID` →
  `{processed_at, note_path, status, flags}`. Idempotent; re-runs skip done memos.
- **Activity log:** append one line to `Claude/System/Activity/<YYYY-MM-DD> Activity Log.md`
  (create from template if missing) under source tag `[voice-memos]`:
  `- HH:MM [voice-memos] Transcribed "{title}" ({dur}) → {date} daily note. {clean | N flags}`.

## Trigger — launchd

`~/Library/LaunchAgents/com.alexpriest.voice-memo-transcribe.plist`:
- `WatchPaths` → the Recordings dir (fires on change).
- `StartCalendarInterval` → daily catch-up (e.g. 06:30) for memos that synced while asleep.
- `ProgramArguments` → `/bin/bash <tool>/run.sh`; `WorkingDirectory` = vault;
  `EnvironmentVariables` PATH (incl. `~/.local/bin`, `/opt/homebrew/bin`) + HOME.
- Logs → `~/Library/Logs/voice-memo-transcribe.log`. `RunAtLoad` false.
- **Concurrency guard:** `run.sh` takes a lockfile (single instance), sleeps ~25s to let
  iCloud/recording writes settle, then processes only finalized + unprocessed memos. Cheap no-op
  when nothing is new (WatchPaths can fire many times mid-recording).

## Call gate (added 2026-07-07)

Both runners skip while the **camera or mic is in use** (Alex on a video call / any call /
recording). Motivation: the rename runner drives the Voice Memos GUI and its idle gate makes it
*likely* to fire mid-call (you sit idle at the keyboard while listening), stealing focus — and
whisper competes with the call for the Neural Engine.

- **Signal:** `bin/callguard`, a compiled Swift helper reading `kCMIODevicePropertyDeviceIsRunningSomewhere`
  (CoreMediaIO / camera) + `kAudioDevicePropertyDeviceIsRunningSomewhere` (CoreAudio / mic). State-only
  reads → no capture session, no TCC permission, no privacy indicator. Verified against a real call:
  Studio Display Camera + AirPods read running=1; virtual "Teams Audio" reads 0 (no stuck-on).
- **Wiring:** `on_call()` in the orchestrator. Gates the `--daemon` run (defer before settle) and the
  `--rename-queue` run (skip pre-pass + abort mid-pass if a call starts).
- **Fails open:** missing/erroring helper → proceed, never a silent permanent halt. `--force` bypasses.
- **Recovery:** deferred memos picked up by the next Recordings event or the 06:30 catch-up.

## Files

```
~/Code/tools/voice-memo-transcribe/
  SPEC.md                                  # this doc
  README.md                                # what it is, install, ops, troubleshooting
  run.sh                                   # launchd entrypoint (bash + lockfile + settle)
  process_voice_memos.py                   # orchestrator (detect, transcribe, enrich, place)
  bin/callguard.swift                      # camera/mic-in-use probe (source)
  bin/callguard                            # compiled helper (gitignored; built by install.sh)
  requirements.txt                         # mlx-whisper
  install.sh                               # venv, deps, model, callguard build, plist load, gitignore
  com.alexpriest.voice-memo-transcribe.plist   # plist template (paths substituted on install)
  .gitignore                               # .venv/, state/, *.log, bin/callguard
  state/processed.json                     # ledger (gitignored)
```

Vault side: `System/Voice Memos/Audio/` (gitignored) holds the audio copies.

## Failure handling

| Failure | Behavior |
|---------|----------|
| DB locked | Copy-with-retry; if still failing, log + clean exit |
| whisper error on a file | Log, mark ledger `status:error`, skip that memo (don't block others), retry next run |
| `claude -p` error / no JSON | Write raw transcript under the toggle + "(enrichment failed)" marker + one heads-up text |
| Overlapping launchd fires | Lockfile → single instance; everything idempotent |
| Mac asleep at memo sync | Daily catch-up run picks it up |

## Privacy

Audio never leaves the device (local whisper, gitignored). Transcript text is committed to the
**private** vault git repo. No third-party service touches the recordings.

## One-time backfill

`process_voice_memos.py --backfill-since YYYY-MM-DD` processes recent memos on demand
(used once at go-live to transcribe today's so Alex sees it working). Default behavior never
touches history.
