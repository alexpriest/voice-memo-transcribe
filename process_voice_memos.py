#!/usr/bin/env python3
"""Detect new Apple Voice Memos, transcribe them on-device, enrich with Claude,
and write them into the right Obsidian daily note under a collapsible toggle.

See SPEC.md for the full design. Runs on the Mac Mini via launchd (event-driven),
or by hand for backfill/testing.

Modes:
  (default)                 process memos whose ID is not yet in the ledger
  --backfill-since DATE      process memos recorded on/after DATE (YYYY-MM-DD),
                             even if previously seeded
  --seed-ledger              mark every current memo as seen WITHOUT transcribing
                             (run once at install so history is ignored)
  --raw                      skip Claude enrichment; write the raw transcript
  --no-notify                never send an iMessage (report what it would send)
  --dry-run                  do everything except write files / update ledger / notify
  --limit N                  process at most N memos this run
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------------- #
HOME = Path.home()
VAULT = HOME / "Obsidian" / "alexpriest"
RECORDINGS_DIR = (
    HOME / "Library" / "Group Containers"
    / "group.com.apple.VoiceMemos.shared" / "Recordings"
)
DB_PATH = RECORDINGS_DIR / "CloudRecordings.db"

TOOL_DIR = Path(__file__).resolve().parent
LEDGER_PATH = TOOL_DIR / "state" / "processed.json"
LOCK_PATH = TOOL_DIR / "state" / ".lock"
RENAME_NOTIFY_PATH = TOOL_DIR / "state" / "rename_notify.json"  # backlog-nudge dedupe state
FATAL_NOTIFY_PATH = TOOL_DIR / "state" / "fatal_notify.json"  # outage-nudge dedupe state
CALLGUARD_BIN = TOOL_DIR / "bin" / "callguard"  # camera/mic-in-use probe (see bin/callguard.swift)
SETTLE_SECONDS = 25  # let recording / iCloud writes finish before reading the container
AUDIO_DEST = VAULT / "System" / "Voice Memos" / "Audio"
DAILY_DIR = VAULT / "Daily"
ACTIVITY_DIR = VAULT / "Claude" / "System" / "Activity"

# Where a finished memo is written: "craft" (a toggle on the Craft daily note,
# which surfaces in Obsidian via the # Journal transclusion) or "obsidian" (the
# original # Voice Memos callout in the vault daily note). Override with VOICE_DEST.
DEST = os.environ.get("VOICE_DEST", "craft")

PHONE = "+12702871307"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
CLAUDE_BIN = str(HOME / ".local" / "bin" / "claude")
CLAUDE_MODEL = "claude-sonnet-4-6"

CORE_DATA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01
MIN_DURATION_S = 3.0
MIN_MTIME_AGE_S = 30  # file must have been still for this long (finished syncing)

PROJECT_NOUNS = ["Cartographer", "Duckbill", "Firesale", "Anthimeros", "Syntensor", "Halcie"]

# Alex's most-mentioned people. Linked CONFIDENTLY (no "?", no flag/notify) even when
# whisper garbles the name into a close phonetic variant. Toddler names especially get mangled.
# "refs" lists how Alex says them in speech + likely mis-hearings.
INNER_CIRCLE = [
    {"note": "Miranda Gale", "refs": ["Miranda", "Mir"], "who": "Alex's wife"},
    {"note": "Halcyon Priest",
     "refs": ["Halcie", "Halcyon", "Hossie", "Halcey", "Hosie", "Halsey", "Howie"],
     "who": "Alex's 2yo daughter (NOT the Halcie hardware project — context decides)"},
    {"note": "Zephyr Priest", "refs": ["Zephyr", "Zeph", "Zef"], "who": "Alex's 4yo son"},
    {"note": "Jack Villani", "refs": ["Jack"], "who": "friend"},
    {"note": "AJ Adams", "refs": ["AJ", "A.J."], "who": "best friend, godparent to the kids"},
    {"note": "Ashley Adams", "refs": ["Ash", "Ashley"],
     "who": "AJ's wife, family friend (default for 'Ash' unless context clearly means a work 'Ash')"},
]

# App rename runner (--rename-queue): applies queued titles by writing THROUGH Core Data via
# the vmrename helper, so Core Data logs a history transaction and voicememod exports it to
# iCloud. No UI, no stolen focus, no idle/unlock gating. Needs Full Disk Access. See
# bin/vmrename.swift and craft-mirror-style notes in README.
RENAME_LOCK_PATH = TOOL_DIR / "state" / ".rename.lock"
VMRENAME_BIN = TOOL_DIR / "bin" / "vmrename"

# Whisper segment confidence thresholds -> "shaky" span
LOW_AVG_LOGPROB = -1.0
HIGH_NO_SPEECH = 0.6
HIGH_COMPRESSION = 2.4


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def acquire_lock(path: Path = LOCK_PATH):
    """Single-instance guard via flock (auto-released on process exit — no stuck locks).
    Returns the held file object, or None if another instance holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fd.close()
        return None
    return fd


# ---------------------------------------------------------------------------- #
# Ledger
# ---------------------------------------------------------------------------- #
def load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except json.JSONDecodeError:
            log("WARN ledger corrupt, starting fresh")
    return {}


def save_ledger(ledger: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False))
    tmp.replace(LEDGER_PATH)


# ---------------------------------------------------------------------------- #
# Detect
# ---------------------------------------------------------------------------- #
def query_memos(min_duration: float = MIN_DURATION_S) -> list[dict]:
    """Copy the live DB (+wal/+shm) to temp and read it read-only."""
    if not DB_PATH.exists():
        # RAISE, don't return [] — a quiet empty list looks like "no new memos" and the
        # failure guard never fires. Note exists() can also be False because TCC denied
        # the stat, so this is not necessarily a missing file: in the 2026-07-23 outage
        # stat() was permitted while open() was not, and a different TCC state would land
        # here instead of on the PermissionError path.
        raise FileNotFoundError(f"no readable CloudRecordings.db at {DB_PATH}")
    with tempfile.TemporaryDirectory() as td:
        tmp_db = Path(td) / "CloudRecordings.db"
        for suffix in ("", "-wal", "-shm"):
            src = DB_PATH.parent / (DB_PATH.name + suffix)
            if src.exists():
                shutil.copy2(src, str(tmp_db) + suffix)
        uri = f"file:{tmp_db}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT ZUNIQUEID    AS uid,
                   ZENCRYPTEDTITLE AS title,
                   ZCUSTOMLABEL AS label,
                   ZDATE        AS zdate,
                   ZDURATION    AS duration,
                   ZPATH        AS path
            FROM ZCLOUDRECORDING
            WHERE (ZPATH LIKE '%.m4a' OR ZPATH LIKE '%.qta')
              AND ZDURATION >= ?
              -- Rows in Recently Deleted still sit in this table. They have no row in the
              -- app's list, so a rename can never land and the runner retries them forever.
              AND ZEVICTIONDATE IS NULL
            ORDER BY ZDATE ASC
            """,
            (min_duration,),
        ).fetchall()
        con.close()

    memos = []
    for r in rows:
        if not r["uid"] or r["zdate"] is None:
            continue
        recorded = datetime.fromtimestamp(r["zdate"] + CORE_DATA_EPOCH)
        title = (r["title"] or "").strip() or (r["label"] or "").strip() or "Voice Memo"
        audio = RECORDINGS_DIR / r["path"]
        memos.append({
            "uid": r["uid"],
            "title": title,
            "recorded": recorded,
            "duration": float(r["duration"] or 0.0),
            "audio": audio,
        })
    return memos


def is_file_ready(audio: Path) -> bool:
    if not audio.exists() or audio.stat().st_size == 0:
        return False
    age = datetime.now().timestamp() - audio.stat().st_mtime
    return age >= MIN_MTIME_AGE_S


# ---------------------------------------------------------------------------- #
# Transcribe
# ---------------------------------------------------------------------------- #
def transcribe(audio: Path) -> dict:
    import mlx_whisper  # imported lazily so --seed-ledger needs no model

    result = mlx_whisper.transcribe(str(audio), path_or_hf_repo=WHISPER_MODEL)
    segments = result.get("segments", [])
    shaky = []
    annotated = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        low = (
            seg.get("avg_logprob", 0.0) < LOW_AVG_LOGPROB
            or seg.get("no_speech_prob", 0.0) > HIGH_NO_SPEECH
            or seg.get("compression_ratio", 0.0) > HIGH_COMPRESSION
        )
        annotated.append(f"[LOW-CONFIDENCE] {text}" if low else text)
        if low:
            shaky.append(text)
    return {
        "text": result.get("text", "").strip(),
        "annotated": "\n".join(annotated),
        "shaky": shaky,
    }


# ---------------------------------------------------------------------------- #
# Enrich (Claude)
# ---------------------------------------------------------------------------- #
def build_entities() -> str:
    # Only include people who have an alias set — the curated shortlist Alex actually refers
    # to by nickname in speech. Dumping all ~1100 People notes bloats the prompt and times the
    # model out for no benefit. An alias is Alex's opt-in: "link this person in my memos."
    lines = []
    people_dir = VAULT / "People"
    inner_notes = {p["note"] for p in INNER_CIRCLE}
    if people_dir.exists():
        for md in sorted(people_dir.glob("*.md")):
            if md.stem.startswith("_") or md.stem in inner_notes:
                continue
            aliases = parse_aliases(md)
            if aliases:
                lines.append(f"- {md.stem} ({', '.join(aliases)})")
    people_block = "\n".join(lines) if lines else "(none)"

    inner = "\n".join(
        f"- [[{p['note']}]] — say/mishear: {', '.join(p['refs'])} ({p['who']})"
        for p in INNER_CIRCLE
    )
    return (
        "INNER CIRCLE — Alex's most-mentioned people. Resolve these CONFIDENTLY: link with the "
        "exact note name, NO trailing '?', and do NOT flag or notify — even when the speech model "
        "garbled the name into a close phonetic variant (e.g. 'Hossie' or 'Halcey' => [[Halcyon "
        "Priest]]). Use context (a memo about home/kids/family => the family members):\n"
        f"{inner}\n\n"
        "OTHER KNOWN PEOPLE (link the exact note name when clearly referenced; parenthetical = "
        "how Alex refers to them). For these, if a reference is a genuine guess, link it but add a "
        "trailing '?' and flag it:\n"
        f"{people_block}\n\n"
        f"KNOWN PROJECTS (link these exact names): {', '.join(PROJECT_NOUNS)}"
    )


def parse_aliases(md: Path) -> list[str]:
    try:
        text = md.read_text(errors="ignore")
    except OSError:
        return []
    m = re.search(r"^aliases:\s*(.*?)(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    block = m.group(1)
    inline = re.match(r"\s*\[(.*?)\]", block)
    if inline:
        items = inline.group(1).split(",")
    else:
        items = re.findall(r"-\s*(.+)", block)
    return [i.strip().strip("'\"") for i in items if i.strip()][:6]


PROMPT_TEMPLATE = """You are cleaning up a transcribed voice memo for Alex Priest's Obsidian vault. \
You are a careful transcription editor, not a writer. Output STRICT JSON only.

MEMO: "{title}" recorded {when} ({dur}).

RAW TRANSCRIPT (segments tagged [LOW-CONFIDENCE] were flagged by the speech model as shaky — \
scrutinize those words; they are the likeliest errors):
---
{transcript}
---

{entities}

YOUR JOB:
1. Write a short, specific, lightly clever TITLE (4-8 words) capturing the memo's main content. \
No date, no quotes, no trailing punctuation. Sentence case. E.g. "Bike-ride debrief and a messy, \
productive morning".
2. Lightly clean the transcript: add punctuation/capitalization, fix obvious mis-hearings, \
remove filler ("um", "uh", false starts), break into paragraphs. NEVER change meaning, add \
content, or summarize. Keep Alex's words.
3. Wrap any person or project from the lists above in [[Exact Note Name]] when clearly referenced. \
Use ONLY names from those lists — never invent a link. Follow the INNER CIRCLE rule: resolve those \
people confidently (no "?", no flag) including obvious phonetic manglings. For OTHER people, if a \
reference is a genuine guess, link it with a trailing "?" and flag it.
4. Mark any genuinely garbled/unintelligible span as [unclear: "your best guess"].
5. Decide should_notify: true ONLY if there is a meaningful uncertainty worth Alex's eyes \
(a garbled word that changes meaning, or a guessed link to a NON-inner-circle person). \
Inner-circle phonetic resolutions and trivially clean memos => false.
6. If notifying, write notify_text: a PLAIN-TEXT SMS (no markdown, no asterisks, under 320 chars) \
naming the memo title, its time, and the specific flags.

NOTIFY_ENABLED: {notify_enabled}
{notify_instruction}

Never use emojis anywhere — not in the title, the transcript, the flags, or the SMS.

OUTPUT — use EXACTLY this layout, nothing before or after it. Do not use JSON. The body after \
the marker is freeform (put the whole cleaned transcript there, no escaping needed):
TITLE: <short clever title>
NOTIFY: <yes or no>
NOTIFY_TEXT: <the SMS if NOTIFY is yes, otherwise leave blank>
FLAGS: <each flag separated by " | ", or blank if none>
===BODY===
<the cleaned transcript with [[wikilinks]] and [unclear: ...] markers — multiple paragraphs>
"""


def enrich(memo: dict, transcription: dict, entities: str, notify_enabled: bool) -> dict | None:
    when = memo["recorded"].strftime("%A %-I:%M %p")
    if notify_enabled:
        notify_instruction = (
            f"If should_notify is true, SEND notify_text now via the "
            f"mcp__kit-tools__send_message tool to {PHONE} (and only that number). "
            f"Use no other tools and write no files."
        )
    else:
        notify_instruction = (
            "Do NOT send any message and do NOT use any tools — only return the output "
            "(still fill NOTIFY_TEXT with what you would have sent)."
        )

    prompt = PROMPT_TEMPLATE.format(
        title=memo["title"],
        when=when,
        dur=fmt_duration(memo["duration"]),
        transcript=transcription["annotated"] or transcription["text"],
        entities=entities,
        notify_enabled=str(notify_enabled).lower(),
        notify_instruction=notify_instruction,
    )

    cmd = [
        CLAUDE_BIN, "-p",
        "--permission-mode", "bypassPermissions",
        "--model", CLAUDE_MODEL,
        "--max-budget-usd", "0.50",
        prompt,
    ]
    # Retry: claude -p can fail transiently (flaky API moment). One hiccup must not
    # demote a memo to a raw transcript.
    for attempt in range(1, 4):
        try:
            proc = subprocess.run(
                cmd, cwd=str(VAULT), capture_output=True, text=True, timeout=420
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            log(f"  enrich attempt {attempt} error: {e}")
        else:
            if proc.returncode == 0:
                parsed = parse_enrich(proc.stdout)
                if parsed:
                    return parsed
                log(f"  enrich attempt {attempt}: unparseable stdout: {proc.stdout[:200]!r}")
            else:
                log(f"  enrich attempt {attempt}: exit {proc.returncode} "
                    f"stdout={proc.stdout[:200]!r} stderr={proc.stderr[:200]!r}")
        if attempt < 3:
            time.sleep(4)
    return None


def parse_enrich(raw: str) -> dict | None:
    """Parse the delimiter-based enrich output. Robust for arbitrarily long bodies
    (the transcript lives after ===BODY=== as freeform text — no JSON escaping)."""
    if "===BODY===" not in raw:
        log(f"  enrich: no BODY marker in: {raw[:160]!r}")
        return None
    head, body = raw.split("===BODY===", 1)
    body = body.strip()
    if not body:
        return None
    fields = {"title": "", "notify": "", "notify_text": "", "flags": ""}
    keys = (("title", "TITLE:"), ("notify", "NOTIFY:"),
            ("notify_text", "NOTIFY_TEXT:"), ("flags", "FLAGS:"))
    for line in head.splitlines():
        for key, prefix in keys:
            if line.strip().upper().startswith(prefix):
                fields[key] = line.split(":", 1)[1].strip()
                break
    flags = [f.strip() for f in fields["flags"].split("|") if f.strip()]
    return {
        "title": fields["title"],
        "cleaned_markdown": body,
        "uncertainties": flags,
        "should_notify": fields["notify"].lower().startswith("y"),
        "notify_text": fields["notify_text"],
    }


# ---------------------------------------------------------------------------- #
# Place
# ---------------------------------------------------------------------------- #
def ordinal(n: int) -> str:
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    m, s = divmod(s, 60)
    return f"{m}m {s}s" if m else f"{s}s"


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip()
    s = re.sub(r"\s+", " ", s)
    if len(s) > 50:
        s = s[:50].rsplit(" ", 1)[0]  # trim to a word boundary, not mid-word
    return s or "voice-memo"


def daily_note_path(dt: datetime) -> Path:
    return DAILY_DIR / f"{dt:%Y}" / f"{dt:%m-%B}" / f"{dt:%Y-%m-%d}.md"


def daily_note_stub(dt: datetime) -> str:
    title = f"{dt:%A, %B} {ordinal(dt.day)}, {dt:%Y}"
    return (
        "---\n"
        "tags:\n  - periodic/daily\n"
        f"title:\n  - {title}\n"
        f'daily-date: "{dt:%Y-%m-%d}T00:00:00"\n'
        "cssclasses:\n  - hide-properties\n"
        "---\n"
        # Keep in sync with System/Templates/Daily Note Template.md: the Journal
        # section transcludes the mirrored Craft daily note (his journaling home).
        f"# Journal\n\n![[Craft/Daily Notes/{dt:%Y}/{dt:%m-%B}/{dt:%Y-%m-%d}]]\n\n---\n\n# Jots\n"
    )


def build_callout(memo: dict, audio_filename: str, body: str,
                  uncertainties: list[str], title: str) -> str:
    header = (
        f"> [!note]- {title} — "
        f"{memo['recorded']:%-I:%M %p} · {fmt_duration(memo['duration'])}"
    )
    lines = [header, f"> ![[{audio_filename}]]", ">"]
    for ln in body.strip().splitlines():
        lines.append(f"> {ln}" if ln.strip() else ">")
    if uncertainties:
        lines.append(">")
        lines.append(f"> *Flagged: {'; '.join(uncertainties)}*")
    return "\n".join(lines)


def insert_callout(note_path: Path, callout: str, uid: str) -> None:
    """Ensure a '# Voice Memos' section exists and add the callout at the end of it.
    Idempotent: if a callout for this memo already exists (identified by the memo's stable
    short-id embedded in the audio filename), replace that callout block in place — no
    visible marker needed."""
    if note_path.exists():
        text = note_path.read_text()
    else:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        dt = datetime.strptime(note_path.stem, "%Y-%m-%d")
        text = daily_note_stub(dt)

    shortid = uid.split("-")[0][:8]
    lines = text.split("\n")
    embed_idx = next((i for i, ln in enumerate(lines) if f"{shortid}.m4a]]" in ln), None)
    if embed_idx is not None:
        # a callout is a contiguous run of lines starting with '>'; expand to its bounds
        start = embed_idx
        while start > 0 and lines[start - 1].startswith(">"):
            start -= 1
        end = embed_idx
        while end + 1 < len(lines) and lines[end + 1].startswith(">"):
            end += 1
        new_lines = lines[:start] + callout.split("\n") + lines[end + 1:]
        note_path.write_text("\n".join(new_lines))
        return

    block = "\n" + callout + "\n"
    if "# Voice Memos" not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += "\n# Voice Memos\n" + block
    else:
        # insert just before the next top-level heading after '# Voice Memos', else EOF
        idx = text.index("# Voice Memos")
        after = text[idx + len("# Voice Memos"):]
        m = re.search(r"\n# ", after)
        if m:
            cut = idx + len("# Voice Memos") + m.start() + 1  # keep the '\n'
            text = text[:cut] + block.lstrip("\n") + "\n" + text[cut:]
        else:
            if not text.endswith("\n"):
                text += "\n"
            text += block
    note_path.write_text(text)


def pretty_audio_name(memo: dict, title: str) -> str:
    shortid = memo["uid"].split("-")[0][:8]
    return f"{memo['recorded']:%Y-%m-%d %H%M} {slugify(title)} {shortid}.m4a"


def place_in_craft(memo: dict, body: str, uncertainties: list[str], title: str,
                   prev_toggle_id: str | None = None) -> str:
    """Write the memo as a toggle on the Craft daily note (transcript + audio inside).

    Returns the toggle's block id — store it in the ledger so a reprocess replaces
    this group instead of appending a duplicate.
    """
    import craft_write
    shortid = memo["uid"].split("-")[0][:8]
    return craft_write.write_voice_memo(
        date=f"{memo['recorded']:%Y-%m-%d}",
        title=title,
        time_label=f"{memo['recorded']:%-I:%M %p}",
        duration_label=fmt_duration(memo["duration"]),
        transcript=body,
        uncertainties=uncertainties,
        audio_path=str(memo["audio"]),
        pretty_name=pretty_audio_name(memo, title),
        shortid=shortid,
        prev_toggle_id=prev_toggle_id,
    )["toggle_id"]


def copy_audio(memo: dict, title: str) -> str:
    AUDIO_DEST.mkdir(parents=True, exist_ok=True)
    shortid = memo["uid"].split("-")[0][:8]
    filename = f"{memo['recorded']:%Y-%m-%d %H%M} {slugify(title)} {shortid}.m4a"
    dest = AUDIO_DEST / filename
    # drop stale copies for this memo (title — and thus filename — can change on reprocess)
    for old in AUDIO_DEST.glob(f"*{shortid}.m4a"):
        if old.name != filename:
            old.unlink()
    if not dest.exists():
        shutil.copy2(memo["audio"], dest)
    return filename


# --------------------------------------------------------------------------- #
# App rename runner (GUI automation → real File>Rename → syncs via CloudKit)
# --------------------------------------------------------------------------- #
def on_call() -> tuple[bool, str]:
    """(True, reason) if the camera or mic is currently in use by any app — i.e. Alex is
    on a video call (camera) or any call/recording (mic). Used to keep this automation from
    stealing focus into the Voice Memos UI or hogging the Neural Engine mid-call.

    Reads hardware state via the compiled `bin/callguard` helper (CoreMediaIO + CoreAudio
    'IsRunningSomewhere' — no capture session, no TCC prompt). FAILS OPEN: if the helper is
    missing or errors, returns (False, ...) so a broken probe never permanently halts the
    pipeline — better to occasionally run during a call than to silently stop transcribing."""
    if not CALLGUARD_BIN.exists():
        return False, "callguard helper missing — not gating"
    try:
        proc = subprocess.run([str(CALLGUARD_BIN)], capture_output=True, text=True, timeout=8)
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"callguard error ({e}) — not gating"
    if proc.returncode != 0:
        return False, f"callguard exit {proc.returncode} — not gating"
    out = proc.stdout.strip()
    active = [name for name, token in (("camera", "camera=1"), ("mic", "mic=1")) if token in out]
    if active:
        return True, " + ".join(active) + " in use"
    return False, out


def _send_sms(text: str) -> bool:
    """Send a plain-text iMessage to Alex via the vault's kit-tools MCP — the same path
    enrich() uses to notify. Returns True on success."""
    prompt = (
        f"Use the mcp__kit-tools__send_message tool to send this EXACT text to {PHONE} "
        f"(and only that number). Send it verbatim, use no other tools, and write no files.\n\n"
        f"{text}"
    )
    cmd = [CLAUDE_BIN, "-p", "--permission-mode", "bypassPermissions",
           "--model", CLAUDE_MODEL, "--max-budget-usd", "0.20", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(VAULT), capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"rename: notify send error: {e}")
        return False
    if proc.returncode != 0:
        log(f"rename: notify exit {proc.returncode} stderr={proc.stderr[:160]!r}")
        return False
    return True


def extract_model() -> Path:
    """Dump Voice Memos' Core Data model out of the live store, fresh, every run.

    Writing THROUGH this model (see bin/vmrename.swift) makes Core Data emit the
    persistent-history transaction that the CloudKit mirroring delegate exports — so a
    rename reaches the iPhone. A raw SQL UPDATE writes no history and never syncs.

    Never cache the .mom: if it drifts from the live store, opening with it would trigger
    a migration that drops columns. The Swift side hard-gates on compatibility, but
    re-extracting each run keeps them in lockstep so the gate never even trips.
    """
    mc = TOOL_DIR / "state" / "model_cache.bin"
    mom = TOOL_DIR / "state" / "VMModel.mom"
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "src.db"
        shutil.copy2(DB_PATH, db)
        con = sqlite3.connect(db)
        blob = con.execute("SELECT Z_CONTENT FROM Z_MODELCACHE").fetchone()[0]
        con.close()
    mc.write_bytes(blob)
    mom.write_bytes(zlib.decompress(blob, -15))
    return mom


def rename_queue(args) -> int:
    """Apply queued titles by writing THROUGH Core Data — no UI, no stolen focus.

    Replaced the old File>Rename accessibility automation (2026-07-23). The Core Data
    write is what a real rename does under the hood: Core Data logs a history transaction
    and voicememod exports it to iCloud. Because nothing touches the screen, all the old
    unlock/idle/on-call gating is gone — this can run headless any time.
    """
    lock = acquire_lock(RENAME_LOCK_PATH)
    if lock is None:
        log("rename: another pass in progress")
        return 0

    ledger = load_ledger()
    live = {m["uid"]: m["title"] for m in query_memos(min_duration=0.0)}  # excludes Recently Deleted
    # Gate on app_renamed + a title, NOT on status: --retitle-all preserves a memo's prior
    # status (e.g. "seeded"), so a status filter would wrongly skip named memos.
    pending = sorted(
        (uid for uid, e in ledger.items() if e.get("title") and not e.get("app_renamed")),
        key=lambda u: ledger[u].get("recorded") or "",
    )

    # Build uid -> "YYYY-MM-DD — title", dropping anything already correct or gone.
    plan: dict[str, str] = {}
    for uid in pending:
        e = ledger[uid]
        date_prefix = (e.get("recorded") or "")[:10] or datetime.now().strftime("%Y-%m-%d")
        target = f"{date_prefix} — {e['title']}"
        if uid not in live:
            log(f"rename: {uid[:8]} gone from app (deleted) — marking done")
            e["app_renamed"] = True
        elif live[uid] == target:
            e["app_renamed"] = True  # already carries the right title
        else:
            plan[uid] = target
    save_ledger(ledger)

    if not plan:
        log("rename: nothing pending")
        return 0

    mom = extract_model()
    with tempfile.TemporaryDirectory() as td:
        batch = Path(td) / "renames.tsv"
        batch.write_text("".join(f"{uid}\t{title}\n" for uid, title in plan.items()))
        cmd = [str(VMRENAME_BIN), str(mom), str(DB_PATH), "--batch", str(batch)]
        if args.dry_run:
            cmd.append("--dry-run")
        log(f"rename: writing {len(plan)} title(s) via Core Data{' (dry-run)' if args.dry_run else ''}")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    for line in proc.stdout.splitlines():
        log(f"  {line}")
    if proc.returncode not in (0, 7):  # 7 = some rows failed; still process the successes
        log(f"rename: vmrename failed rc={proc.returncode}: {proc.stderr.strip()[:200]}")
        return 1

    if args.dry_run:
        return 0

    # Mark done only the uids vmrename reported OK, then confirm against the DB.
    ok_uids = {ln.split()[1] for ln in proc.stdout.splitlines() if ln.startswith("OK ")}
    fresh = {m["uid"]: m["title"] for m in query_memos(min_duration=0.0)}
    done = 0
    for uid in ok_uids:
        if fresh.get(uid) == plan[uid]:
            ledger[uid]["app_renamed"] = True
            done += 1
        else:
            log(f"rename: {uid[:8]} saved but DB shows {fresh.get(uid)!r} — leaving pending")
    save_ledger(ledger)
    log(f"rename: pass done, {done} applied")
    return 0


# The Kit Activity Log is read back by harvesting every line that starts with
# "- " straight into an LLM system prompt, and the title interpolated below is
# LLM-generated from the (attacker-influenceable) transcript — so an embedded
# newline could forge a second well-formed bullet into another persona's
# instructions. This block implements the canonical sanitizeLogText spec (v5)
# shared by every writer of this log, in both Python and TypeScript. The order
# of operations and the character classes must stay byte-identical across all
# writers — a differential test runs the same vector file against each
# implementation.

# Spec step 2: every character that starts a new line for a `split("\n")`
# reader, for `str.splitlines()`, or for an LLM reading the file, flattened to
# a single space. \x1C-\x1E are `str.splitlines()` breaks and belong here;
# \x1F is NOT a line break, so step 3 below deletes it outright instead of
# leaving a space. The set is spelled out explicitly (not via `\s`) so it is
# stated in the same form in every writer: JS `\s` does not match \x1C-\x1E.
_LINE_BREAKS = re.compile("[\r\n\x0B\x0C\x1C\x1D\x1E\x85\u2028\u2029]+")

# Spec step 3: remaining C0 controls and DEL are deleted with no replacement
# (this catches \x1F; \t is whitespace and handled by the WS passes below;
# \x1C-\x1E were already flattened in step 2).
_CONTROLS = re.compile("[\x00-\x08\x0E-\x1F\x7F]")

# The explicit whitespace class shared byte-for-byte with the TS writer. NOT
# `\s`: Python's `\s` also matches \x1C-\x1F (handled above) and misses
# U+FEFF (BOM); JS `\s` misses \x1C-\x1F. The explicit class depends on neither.
_WS = "[ \t\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000\uFEFF]"

# Spec step 4: strip leading markup — runs of whitespace, and runs of [-*+>#]
# ONLY when the run is immediately followed by whitespace or end-of-string.
# "- ", "> ", "  - > # " and a bare "---" strip; "+15551234567", "-3 lbs",
# "*star*", "#kit" and "---divider" survive, because a marker run glued to
# content is content. The older greedy `^[\s\-*+>#]+` ate the leading "+" off
# an E.164 sender and inverted the sign on things like "-3 lbs".
_LEADING_MARKERS = re.compile(f"^(?:{_WS}+|[-*+>#]+(?={_WS}|$))+")

# Spec steps 5-6: collapse internal whitespace runs to one space, trim edges.
_WS_RUNS = re.compile(f"{_WS}+")
_WS_EDGES = re.compile(f"^{_WS}+|{_WS}+$")


def sanitize_log_text(text) -> str:
    """Flatten untrusted text so it cannot forge a log entry.

    Flattening line breaks is the part that closes the forgery hole. Stripping
    leading markers is cosmetic defence-in-depth: this text is interpolated
    after `- HH:MM [voice-memos] `, so a marker in it cannot begin a line for a
    reader that splits on newlines. None becomes ""; any other non-string is
    coerced with str(). No Unicode normalization happens here — content is
    preserved; NFKC is the reader's job.
    """
    text = "" if text is None else str(text)
    text = _LINE_BREAKS.sub(" ", text)
    text = _CONTROLS.sub("", text)
    text = _LEADING_MARKERS.sub("", text)
    text = _WS_RUNS.sub(" ", text)
    return _WS_EDGES.sub("", text)


def append_activity(memo: dict, flags: list[str], title: str) -> None:
    now = datetime.now()
    path = ACTIVITY_DIR / f"{now:%Y-%m-%d} Kit Activity Log.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ncreated: '[[{now:%Y-%m-%d}]]'\ntags: activity-log\n---\n"
            f"# Kit Activity Log — {now:%Y-%m-%d}\n\n"
        )
    # Every interpolated field goes through sanitize_log_text: the title is
    # LLM-generated from the transcript, and the derived fields are sanitized
    # too so the bullet stays one line no matter what upstream code produces.
    safe_title = sanitize_log_text(title)
    safe_duration = sanitize_log_text(fmt_duration(memo["duration"]))
    safe_day = sanitize_log_text(f"{memo['recorded']:%-m/%-d}")
    safe_flag_str = sanitize_log_text(
        f"{len(flags)} flag(s)" if flags else "clean"
    )
    line = (
        f"- {now:%H:%M} [voice-memos] Transcribed \"{safe_title}\" "
        f"({safe_duration}) → {safe_day} daily note. {safe_flag_str}\n"
    )
    with path.open("a") as f:
        f.write(line)


# ---------------------------------------------------------------------------- #
# Failure guard
# ---------------------------------------------------------------------------- #
def fatal_message(exc: BaseException) -> str:
    """Plain-text SMS for a run-killing error. Names the fix, not just the symptom."""
    if isinstance(exc, PermissionError):
        return (
            "Voice memo transcription is DOWN: no Full Disk Access, so it can't read the "
            "Voice Memos database. Fix: System Settings > Privacy & Security > Full Disk "
            f"Access, re-grant {TOOL_DIR}/.venv/bin/python3.11. "
            "Memos are queued and process once it's back."
        )[:315]
    if isinstance(exc, FileNotFoundError):
        return (
            "Voice memo transcription is DOWN: can't see the Voice Memos database. Either "
            "the container moved, or Full Disk Access was revoked (a denied stat looks "
            "identical to a missing file). Check System Settings > Privacy & Security > "
            f"Full Disk Access for {TOOL_DIR}/.venv/bin/python3.11."
        )[:315]
    detail = str(exc)[:120].strip().rstrip(".")
    return (
        f"Voice memo transcription is DOWN: {type(exc).__name__}: {detail}. "
        "Nothing is being transcribed. Log: ~/Library/Logs/voice-memo-transcribe.log"
    )[:315]


def notify_fatal(exc: BaseException) -> None:
    """Text Alex when a run dies outright — ONCE per outage. This job fires on every
    filesystem event in the Recordings dir, so an un-deduped nudge would text him dozens of
    times. Dedupe is keyed on the error signature and cleared by the next successful DB read
    (clear_fatal_notify), so a later outage nudges again. Fails open: a problem in here must
    never become a second failure mode or mask the real one."""
    try:
        signature = f"{type(exc).__name__}: {str(exc)[:200]}"
        state = {}
        if FATAL_NOTIFY_PATH.exists():
            try:
                state = json.loads(FATAL_NOTIFY_PATH.read_text())
            except (ValueError, OSError):
                state = {}
        if state.get("signature") == signature:
            log("fatal: already nudged for this outage — staying quiet")
            return
        body = fatal_message(exc)
        if "--no-notify" in sys.argv or "--dry-run" in sys.argv:
            log(f"fatal: WOULD text: {body!r}")
            return
        if not _send_sms(body):
            return
        FATAL_NOTIFY_PATH.write_text(json.dumps(
            {"signature": signature,
             "last_notified": datetime.now().isoformat(timespec="seconds")}, indent=2))
        log("fatal: nudged Alex")
    except Exception as e:  # noqa: BLE001 - the guard must never mask the real failure
        log(f"fatal: could not notify: {e}")


SHORT_MEMO_TITLE = "Short recording"

# Whisper hallucinates plausible-length gibberish out of silence, so a length check alone
# doesn't catch empty memos — the enricher dutifully writes a title ABOUT the audio quality
# ("Memo too garbled to recover"). Those are useless for finding a memo later; the name iOS
# gave it is a LOCATION and strictly more useful. Detect and fall back.
META_TITLE_RE = re.compile(
    r"garbl|unintelligib|inaudib|indecipher|silence|silent|no speech|nothing but|"
    r"nothing to (say|transcribe|recover|report)|no content|empty|blank|"
    r"too (short|quiet|garbled)|couldn'?t|could not|unclear audio|no audible",
    re.I,
)


def usable_title(generated: str, orig_title: str) -> str:
    """A title that describes the *recording* instead of its content is worse than useless."""
    if generated and not META_TITLE_RE.search(generated):
        return generated
    if orig_title and not orig_title.startswith("New Recording"):
        return orig_title  # iOS location name — at least says where he was
    return SHORT_MEMO_TITLE


def retitle_all(args) -> int:
    """Give every un-dated memo in the app a title, then let --rename-queue apply them.

    Deliberately does NOT write to Craft or Obsidian. Alex asked for his back catalogue
    to be *named*, not for five years of old audio to be backfilled into daily notes that
    never existed. The transcript is generated, used to pick a title, and dropped.

    Memos below MIN_DURATION_S (accidental taps) can't be transcribed, so they get a
    dated placeholder rather than being skipped — leaving them untitled is what let two
    "New Recording 6"s collide and stall the rename runner indefinitely.
    """
    ledger = load_ledger()
    memos = query_memos(min_duration=0.0)  # include the sub-3s taps
    clear_fatal_notify()
    # "already named by us" means a real YYYY-MM-DD prefix. A bare 4-digit test wrongly
    # matched iOS's street-address titles ("7803 Deer Ridge Cir 9"), silently skipping
    # every memo recorded at home -- the exact memos most worth naming.
    dated = re.compile(r"^\d{4}-\d{2}-\d{2}\b")
    todo = [m for m in memos if not dated.match(m["title"])]
    if args.limit:
        todo = todo[: args.limit]
    log(f"{len(memos)} memos in app, {len(todo)} without a dated title")

    # Two taps on the same day would both become "<date> — Short recording", i.e. a brand
    # new duplicate-title collision of exactly the kind this pass exists to clear. Number
    # the repeats so every produced title is unique.
    short_seen: dict[str, int] = {}

    done = 0
    for m in todo:
        entry = dict(ledger.get(m["uid"]) or {})
        if m["duration"] < MIN_DURATION_S:
            day = f"{m['recorded']:%Y-%m-%d}"
            short_seen[day] = short_seen.get(day, 0) + 1
            n = short_seen[day]
            title = SHORT_MEMO_TITLE if n == 1 else f"{SHORT_MEMO_TITLE} {n}"
            log(f"→ {day} {m['title']!r} ({m['duration']:.1f}s) "
                f"— too short to transcribe, titling {title!r}")
        elif entry.get("title") and entry.get("status") in ("done", "titled"):
            # Already transcribed on a previous run (e.g. today's memo, which is written to
            # Craft already) — reuse that title instead of burning the transcription again.
            title = entry["title"]
            log(f"→ {m['recorded']:%Y-%m-%d} {m['title']!r} — already titled {title!r}, reusing")
        else:
            log(f"→ {m['recorded']:%Y-%m-%d} {m['title']!r} ({fmt_duration(m['duration'])})")
            if args.dry_run:
                log("  [dry-run] would transcribe for a title")
                continue
            try:
                tr = transcribe(m["audio"])
            except Exception as e:  # noqa: BLE001 - one bad memo must not kill the pass
                log(f"  transcribe error: {e} — leaving untitled")
                continue
            spoken = (tr.get("text") or "").strip()
            if len(spoken) < 40:
                # Silence or a pocket-recording. A generated title here just describes the
                # emptiness ("Nearly empty memo, barely a word"), which is less useful than
                # the name iOS already gave it -- those are LOCATIONS ("Beaver Creek
                # Resort", "Deer Ridge Cir"), so they at least say where he was.
                title = m["title"]
                if title.startswith("New Recording"):
                    title = SHORT_MEMO_TITLE  # generic name + no speech = nothing to preserve
                log(f"  no real speech ({len(spoken)} chars) — keeping {title!r}")
                enriched = None
            else:
                enriched = None if args.raw else enrich(m, tr, "", notify_enabled=False)
                title = ((enriched or {}).get("title") or "").strip()
            if not title:
                # No enrichment: fall back to the transcript's opening words, which still
                # beats "New Recording 4" for finding a memo later.
                title = " ".join((tr.get("text") or "").split()[:8]).strip(" .,") or m["title"]
            before = title
            title = usable_title(title, entry.get("orig_title") or m["title"])
            if title != before:
                log(f"  {before!r} describes the audio, not the content — using {title!r}")
            log(f"  title: {title!r}")
        if args.dry_run:
            continue
        # Force "titled" -- never preserve a prior "seeded"/"done", or the rename queue's
        # status filter (historically) would skip a freshly-named memo. The queue now gates
        # on app_renamed instead, but keeping status honest avoids future foot-guns.
        entry.update({"status": "done" if entry.get("status") == "done" else "titled",
                      "recorded": m["recorded"].isoformat(timespec="seconds"),
                      "orig_title": entry.get("orig_title") or m["title"],
                      "title": title,
                      "app_renamed": False,
                      "titled_at": datetime.now().isoformat(timespec="seconds")})
        ledger[m["uid"]] = entry
        save_ledger(ledger)
        done += 1

    log(f"titled {done} memo(s) — run --rename-queue to apply them in the app")
    return 0


def clear_fatal_notify() -> None:
    """A successful DB read means the outage is over — drop the dedupe state so the next one
    nudges. Fails open for the same reason as notify_fatal."""
    try:
        FATAL_NOTIFY_PATH.unlink(missing_ok=True)
    except OSError as e:
        log(f"fatal: could not clear notify state: {e}")


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #
def select_memos(memos: list[dict], ledger: dict, args) -> list[dict]:
    if args.uids:
        wanted = {u.strip() for u in args.uids.split(",") if u.strip()}
        out = [m for m in memos if m["uid"] in wanted]  # forced, ignore ledger status
        return out[: args.limit] if args.limit else out
    out = []
    for m in memos:
        entry = ledger.get(m["uid"])
        if args.backfill_since:
            since = datetime.strptime(args.backfill_since, "%Y-%m-%d")
            if m["recorded"] < since:
                continue
            if entry and entry.get("status") == "done":
                continue
        else:
            if entry is not None:  # seeded or done => skip
                continue
        out.append(m)
    if args.limit:
        out = out[: args.limit]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-since")
    ap.add_argument("--uids", help="comma-separated ZUNIQUEIDs to force-process")
    ap.add_argument("--seed-ledger", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--daemon", action="store_true",
                    help="launchd mode: single-instance lock + settle delay")
    ap.add_argument("--rename-queue", action="store_true",
                    help="drain the app-rename backlog via GUI (gated on unlock + idle)")
    ap.add_argument("--force", action="store_true",
                    help="with --rename-queue: bypass the unlock/idle gate (manual test)")
    ap.add_argument("--retitle-all", action="store_true",
                    help="give every un-dated memo in the app a title and queue the rename; "
                         "transcribes ONLY to pick a title — writes nothing to Craft/Obsidian")
    args = ap.parse_args()

    if args.retitle_all:
        return retitle_all(args)

    if args.rename_queue:
        return rename_queue(args)

    if args.daemon:
        lock = acquire_lock()
        if lock is None:
            log("another run in progress — exiting")
            return 0
        active, why = on_call()
        if active:
            log(f"{why} — on a call, deferring (next memo or the 06:30 catch-up retries)")
            return 0
        time.sleep(SETTLE_SECONDS)

    ledger = load_ledger()
    memos = query_memos()
    clear_fatal_notify()  # DB read worked — any prior outage is over
    log(f"{len(memos)} eligible memos in DB")

    if args.seed_ledger:
        seeded = 0
        for m in memos:
            if m["uid"] not in ledger:
                ledger[m["uid"]] = {"status": "seeded",
                                    "seeded_at": datetime.now().isoformat(timespec="seconds")}
                seeded += 1
        save_ledger(ledger)
        log(f"seeded {seeded} memos (history ignored going forward)")
        return 0

    todo = select_memos(memos, ledger, args)
    if not todo:
        log("nothing new to process")
        return 0
    log(f"processing {len(todo)} memo(s)")

    entities = build_entities()
    notify_enabled = not args.no_notify

    for m in todo:
        if not is_file_ready(m["audio"]):
            log(f"  skip (not ready / still syncing): {m['title']}")
            continue
        log(f"→ {m['title']} ({fmt_duration(m['duration'])}, {m['recorded']:%Y-%m-%d %H:%M})")

        try:
            tr = transcribe(m["audio"])
        except Exception as e:  # noqa: BLE001 - never let one memo kill the run
            log(f"  transcribe error: {e}")
            ledger[m["uid"]] = {"status": "error", "error": str(e)[:200],
                                "at": datetime.now().isoformat(timespec="seconds")}
            save_ledger(ledger)
            continue

        uncertainties: list[str] = []
        notify_text = ""
        should_notify = False
        clever_title = m["title"]
        if args.raw:
            body = tr["text"]
        else:
            enriched = enrich(m, tr, entities, notify_enabled)
            if enriched:
                body = enriched.get("cleaned_markdown") or tr["text"]
                clever_title = (enriched.get("title") or "").strip() or m["title"]
                uncertainties = enriched.get("uncertainties") or []
                should_notify = bool(enriched.get("should_notify"))
                notify_text = enriched.get("notify_text") or ""
            else:
                body = tr["text"]
                uncertainties = ["enrichment failed — raw transcript"]
                should_notify = True
                notify_text = (
                    f"Voice memo \"{m['title']}\" ({m['recorded']:%-I:%M%p}) transcribed but "
                    f"auto-cleanup failed; raw text is in the {m['recorded']:%-m/%-d} daily note."
                )

        app_title = f"{m['recorded']:%Y-%m-%d} — {clever_title}"

        if args.dry_run:
            log(f"  [dry-run] dest={DEST}  title: {clever_title!r}  (app: {app_title!r})")
            if DEST == "craft":
                log(f"  [dry-run] would add Craft toggle to daily note {m['recorded']:%Y-%m-%d}:")
                log(f"    ### Voice memo: {clever_title} · {m['recorded']:%-I:%M %p} · {fmt_duration(m['duration'])}")
                log(f"    [transcript {len(body)} chars] + audio {pretty_audio_name(m, clever_title)!r}")
            else:
                log("  [dry-run] would write callout:")
                print(build_callout(m, "AUDIO.m4a", body, uncertainties, clever_title))
            log(f"  [dry-run] should_notify={should_notify} notify_text={notify_text!r}")
            continue

        craft_toggle_id = None
        if DEST == "craft":
            craft_toggle_id = place_in_craft(
                m, body, uncertainties, clever_title,
                prev_toggle_id=(ledger.get(m["uid"]) or {}).get("craft_toggle_id"),
            )
            note_ref = f"Craft/Daily Notes/{m['recorded']:%Y/%m-%B/%Y-%m-%d} (toggle)"
        else:
            audio_filename = copy_audio(m, clever_title)
            note_path = daily_note_path(m["recorded"])
            callout = build_callout(m, audio_filename, body, uncertainties, clever_title)
            insert_callout(note_path, callout, m["uid"])
            note_ref = str(note_path.relative_to(VAULT))
        append_activity(m, uncertainties, clever_title)
        # app rename is handled asynchronously by the --rename-queue runner when the
        # screen is unlocked and idle (see rename_queue); transcription never touches the UI.
        ledger[m["uid"]] = {
            "status": "done",
            "note": note_ref,
            "recorded": m["recorded"].isoformat(timespec="seconds"),
            "orig_title": m["title"],
            "title": clever_title,
            "app_renamed": False,
            "craft_toggle_id": craft_toggle_id,
            "flags": uncertainties,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_ledger(ledger)
        flag_note = f" · {len(uncertainties)} flag(s)" if uncertainties else ""
        notify_note = " · texted Alex" if (should_notify and notify_enabled and not args.raw) else (
            f" · WOULD text: {notify_text!r}" if should_notify else ""
        )
        log(f"  ✓ → {note_ref}{flag_note}{notify_note}")

    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001 - nudge Alex, then die loudly into the log as before
        notify_fatal(e)
        raise
    sys.exit(rc)
