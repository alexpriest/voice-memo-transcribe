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
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
AUDIO_DEST = VAULT / "System" / "Voice Memos" / "Audio"
DAILY_DIR = VAULT / "Daily"
ACTIVITY_DIR = VAULT / "Claude" / "System" / "Activity"

PHONE = "+12702871307"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
CLAUDE_BIN = str(HOME / ".local" / "bin" / "claude")
CLAUDE_MODEL = "claude-sonnet-4-6"

CORE_DATA_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01
MIN_DURATION_S = 3.0
MIN_MTIME_AGE_S = 30  # file must have been still for this long (finished syncing)

PROJECT_NOUNS = ["Cartographer", "Duckbill", "Firesale", "Anthimeros", "Syntensor", "Halcie"]

# Whisper segment confidence thresholds -> "shaky" span
LOW_AVG_LOGPROB = -1.0
HIGH_NO_SPEECH = 0.6
HIGH_COMPRESSION = 2.4


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


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
def query_memos() -> list[dict]:
    """Copy the live DB (+wal/+shm) to temp and read it read-only."""
    if not DB_PATH.exists():
        log(f"ERROR no CloudRecordings.db at {DB_PATH}")
        return []
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
            WHERE ZPATH LIKE '%.m4a'
              AND ZDURATION >= ?
            ORDER BY ZDATE ASC
            """,
            (MIN_DURATION_S,),
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
    lines = []
    people_dir = VAULT / "People"
    count = 0
    if people_dir.exists():
        for md in sorted(people_dir.glob("*.md")):
            if md.stem.startswith("_") or count >= 500:
                continue
            aliases = parse_aliases(md)
            if aliases:
                lines.append(f"- {md.stem} ({', '.join(aliases)})")
            else:
                lines.append(f"- {md.stem}")
            count += 1
    people_block = "\n".join(lines) if lines else "(none found)"
    return (
        "KNOWN PEOPLE (link with the exact note name on the left; the parenthetical "
        "first-names/aliases are how Alex usually refers to them in speech):\n"
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
1. Lightly clean the transcript: add punctuation/capitalization, fix obvious mis-hearings, \
remove filler ("um", "uh", false starts), break into paragraphs. NEVER change meaning, add \
content, or summarize. Keep Alex's words.
2. Wrap any person or project from the KNOWN lists in [[Exact Note Name]] when clearly referenced. \
Use ONLY names from those lists — never invent a link. If a reference is ambiguous or you are \
guessing, still link it but append a literal question mark after the closing brackets: [[Name]]?
3. Mark any genuinely garbled/unintelligible span as [unclear: "your best guess"].
4. Decide should_notify: true ONLY if there is a meaningful uncertainty worth Alex's eyes \
(a garbled word that changes meaning, or a guessed link). Trivially clean memos => false.
5. If notifying, write notify_text: a PLAIN-TEXT SMS (no markdown, no asterisks, under 320 chars) \
naming the memo, its time, and the specific flags.

NOTIFY_ENABLED: {notify_enabled}
{notify_instruction}

OUTPUT — a single minified JSON object and NOTHING else:
{{"cleaned_markdown": "<the cleaned body with wikilinks and [unclear] markers>", \
"uncertainties": ["<short human-readable flag>", ...], \
"should_notify": <true|false>, "notify_text": "<sms text or empty string>"}}
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
            "Do NOT send any message and do NOT use any tools — only return the JSON "
            "(still fill notify_text with what you would have sent)."
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
    try:
        proc = subprocess.run(
            cmd, cwd=str(VAULT), capture_output=True, text=True, timeout=300
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"  enrich failed: {e}")
        return None
    if proc.returncode != 0:
        log(f"  enrich exit {proc.returncode}: {proc.stderr[:300]}")
        return None
    return parse_json(proc.stdout)


def parse_json(raw: str) -> dict | None:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    log(f"  could not parse enrich JSON from: {raw[:200]}")
    return None


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
    return s[:50] or "voice-memo"


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
        "# Morning Pages\n\n*Morning pages go here.*\n\n---\n\n# Jots\n"
    )


def build_callout(memo: dict, audio_filename: str, body: str, uncertainties: list[str]) -> str:
    header = (
        f"> [!note]- 🎙️ {memo['title']} — "
        f"{memo['recorded']:%-I:%M %p} · {fmt_duration(memo['duration'])}"
    )
    lines = [header, f"> ![[{audio_filename}]]", ">"]
    for ln in body.strip().splitlines():
        lines.append(f"> {ln}" if ln.strip() else ">")
    if uncertainties:
        lines.append(">")
        lines.append(f"> *⚠️ {'; '.join(uncertainties)}*")
    return "\n".join(lines)


def insert_callout(note_path: Path, callout: str) -> None:
    """Ensure a '# Voice Memos' section exists and add the callout at the end of it."""
    if note_path.exists():
        text = note_path.read_text()
    else:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        text = None
    if text is None:
        # new note
        dt = datetime.strptime(note_path.stem, "%Y-%m-%d")
        text = daily_note_stub(dt)

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


def copy_audio(memo: dict) -> str:
    AUDIO_DEST.mkdir(parents=True, exist_ok=True)
    shortid = memo["uid"].split("-")[0][:8]
    filename = f"{memo['recorded']:%Y-%m-%d %H%M} {slugify(memo['title'])} {shortid}.m4a"
    dest = AUDIO_DEST / filename
    if not dest.exists():
        shutil.copy2(memo["audio"], dest)
    return filename


def append_activity(memo: dict, flags: list[str]) -> None:
    now = datetime.now()
    path = ACTIVITY_DIR / f"{now:%Y-%m-%d} Kit Activity Log.md"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ncreated: '[[{now:%Y-%m-%d}]]'\ntags: activity-log\n---\n"
            f"# Kit Activity Log — {now:%Y-%m-%d}\n\n"
        )
    flag_str = f"{len(flags)} flag(s)" if flags else "clean"
    line = (
        f"- {now:%H:%M} [voice-memos] Transcribed \"{memo['title']}\" "
        f"({fmt_duration(memo['duration'])}) → {memo['recorded']:%-m/%-d} daily note. {flag_str}\n"
    )
    with path.open("a") as f:
        f.write(line)


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #
def select_memos(memos: list[dict], ledger: dict, args) -> list[dict]:
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
    ap.add_argument("--seed-ledger", action="store_true")
    ap.add_argument("--raw", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    ledger = load_ledger()
    memos = query_memos()
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
        if args.raw:
            body = tr["text"]
        else:
            enriched = enrich(m, tr, entities, notify_enabled)
            if enriched:
                body = enriched.get("cleaned_markdown") or tr["text"]
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

        if args.dry_run:
            log("  [dry-run] would write callout:")
            print(build_callout(m, "AUDIO.m4a", body, uncertainties))
            log(f"  [dry-run] should_notify={should_notify} notify_text={notify_text!r}")
            continue

        audio_filename = copy_audio(m)
        note_path = daily_note_path(m["recorded"])
        callout = build_callout(m, audio_filename, body, uncertainties)
        insert_callout(note_path, callout)
        append_activity(m, uncertainties)
        ledger[m["uid"]] = {
            "status": "done",
            "note": str(note_path.relative_to(VAULT)),
            "title": m["title"],
            "flags": uncertainties,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_ledger(ledger)
        flag_note = f" · {len(uncertainties)} flag(s)" if uncertainties else ""
        notify_note = " · texted Alex" if (should_notify and notify_enabled and not args.raw) else (
            f" · WOULD text: {notify_text!r}" if should_notify else ""
        )
        log(f"  ✓ → {note_path.relative_to(VAULT)}{flag_note}{notify_note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
