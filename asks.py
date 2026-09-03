#!/usr/bin/env python3
"""Callout dispatch — the second pass over a cleaned voice memo (ANT-757).

Alex addresses agents by name inside memos ("Kit, remind me…", "tell Iris…",
"Ansel should write up…"). Until 2026-09-03 those asks waited for the 5 AM
briefing, or for a session that happened to read the memo. This module runs
right after the memo is written to Craft and routes each ask within minutes:

  Kit     → a checkbox on the daily note tagged #kit. kit-watch (launchd, 5 min)
            already dispatches a headless Kit worker on any new #kit block and
            swaps in #review when done. No second dispatcher for Kit.
  persona → an entry in that persona's inbox file (quote + memo ref), a checkbox
            on the daily note marked "→ Name", and a detached headless run of the
            persona (`claude --agent <name> -p`) from its own folder. The run
            answers under the checkbox and tags it #review.
  Iris    → same as any persona, and she runs unattended (Alex, 2026-09-03), but
            her domain is sealed: nothing this module writes outside Confidant/
            and Alex's own Craft note carries her ask text — counts only.

One SMS per memo, only when at least one ask was heard, deduped in the ledger
exactly like the existing flag notification. Every failure here is logged and
swallowed: the memo is already on the page, and a missed ask still reaches the
persona at its next session start through the inbox file.

Stand-alone check against a saved transcript (no writes, no texts):

    .venv/bin/python3.11 asks.py --file /tmp/memo-2026-09-02.txt
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
VAULT = HOME / "Obsidian" / "alexpriest"
CLAUDE_DIR = VAULT / "Claude"
TOOL_DIR = Path(__file__).resolve().parent
DISPATCH_LOG = TOOL_DIR / "state" / "dispatch.log"
CLAUDE_BIN = str(HOME / ".local" / "bin" / "claude")
EXTRACT_MODEL = "claude-sonnet-4-6"
VAULT_APPEND = str(HOME / ".local" / "bin" / "vault-append")
PERSONA_PATH = "/Users/alex/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PERSONA_BUDGET_USD = "10.00"

# name → where the persona lives. "sealed" keeps ask text out of every shared surface.
PERSONAS: dict[str, dict] = {
    "kit":    {"name": "Kit",    "folder": "Chief of Staff", "inbox": None,
               "who": "chief of staff: logistics, scheduling, follow-ups, vendors, anything operational"},
    "iris":   {"name": "Iris",   "folder": "Confidant", "inbox": "confidant.md", "sealed": True,
               "who": "family and relationship confidant: marriage, parenting, personal reflection"},
    "ansel":  {"name": "Ansel",  "folder": "Writing", "inbox": "writing.md",
               "who": "writing partner: posts, drafts, the topic bank, anything he wants written"},
    "mateo":  {"name": "Mateo",  "folder": "Vermouth Expert", "inbox": "vermouth-expert.md",
               "who": "vermouth partner on Cartographer: blends, tastings, the brand"},
    "asa":    {"name": "Asa",    "folder": "Coach", "inbox": "coach.md",
               "who": "training and recovery coach: rides, workouts, sleep, the body"},
    "wren":   {"name": "Wren",   "folder": "Stylist", "inbox": "stylist.md",
               "who": "stylist: outfits, wardrobe, shopping, packing"},
    "paloma": {"name": "Paloma", "folder": "Tutor", "inbox": "tutor.md",
               "who": "Spanish tutor: vocabulary, drills, flashcards"},
    "juno":   {"name": "Juno",   "folder": "Architect", "inbox": "architect.md",
               "who": "systems architect: the agent fleet, daemons, shared docs, anything broken in the system"},
}

_LINE_BREAKS = re.compile(r"[\r\n  \x85]+")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
QUOTE_MAX = 220
ASK_MAX = 160
SMS_MAX = 320


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] asks: {msg}", flush=True)


def flat(text, limit: int | None = None) -> str:
    """One line, no control characters, optional cap. Ask and quote text come from the
    transcript via the model — untrusted — and get interpolated into a Craft block, an
    inbox file with `###` headers, and a log bullet. Flattening closes the forgery hole."""
    text = "" if text is None else str(text)
    text = _CONTROLS.sub("", _LINE_BREAKS.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------- #
# Extraction
# ---------------------------------------------------------------------------- #
def _roster() -> str:
    return "\n".join(f"- {k}: {v['who']}" for k, v in PERSONAS.items())


PROMPT_TEMPLATE = """You are reading a cleaned transcript of a voice memo Alex Priest recorded. \
Your only job is to find the places where he EXPLICITLY hands something to one of his agents by \
name. Output STRICT text in the layout below, nothing else.

MEMO: "{title}" recorded {when} ({dur}).

AGENTS HE MAY ADDRESS (use the lowercase key as ADDRESSEE):
{roster}

WHAT COUNTS AS AN ASK — all of these are asks:
- Direct address: "Kit, remind me to…", "Iris, I want to talk about…"
- Handing off: "tell Ansel to…", "flag this for Kit", "file this for Kit", "I'll leave that with Mateo", \
"Kit can figure out the follow-ups from this memo", "note for Wren".
- A named agent plus a thing he wants done, even loosely: "Asa should look at how the ride felt".

WHAT DOES NOT COUNT — never emit these:
- Mentioning an agent in passing: "I talked to Kit about it yesterday", "the thing Ansel drafted".
- Self-talk with no agent named: "I should look into that", "note to self", "I want to remember…". \
Those are his to act on; do NOT assign them to anyone.
- Anything addressed to a human (Miranda, a friend, a vendor). Only the agents listed above.

RULES:
- One ASK block per distinct ask. If he hands the WHOLE memo to an agent ("file all this for Kit, \
we'll figure out follow-ups"), emit ONE ask whose text names the memo's main threads, e.g. \
"Pull the follow-ups from this memo: the vermouth platform idea, the Priest name, consulting math".
- ASK is imperative, specific, under 140 characters, written for the agent ("Remind Alex to…", \
"Draft…", "Look into…"). Never invent detail that is not in the transcript.
- QUOTE is his words, verbatim from the transcript, the sentence or two that contain the address. \
Under 220 characters.
- If there are no asks, output exactly: NONE

TRANSCRIPT:
---
{transcript}
---

OUTPUT — exactly this layout, repeated per ask, no other text before or after:
ASK
ADDRESSEE: <lowercase key from the list>
ASK: <imperative ask>
QUOTE: <his words>
END
"""


def parse_asks(raw: str) -> list[dict]:
    """Parse the ASK/END blocks. Unknown addressees and empty asks are dropped."""
    out: list[dict] = []
    if not raw or raw.strip().upper() == "NONE":
        return out
    for block in re.findall(r"^ASK\s*\n(.*?)^END\s*$", raw, re.M | re.S):
        fields = {"addressee": "", "ask": "", "quote": ""}
        for line in block.splitlines():
            head, sep, rest = line.partition(":")
            key = head.strip().lower()
            if sep and key in fields:
                fields[key] = rest.strip()
        addressee = fields["addressee"].lower().strip("#@ ")
        ask = flat(fields["ask"], ASK_MAX).strip('"“”').rstrip(".")
        fields["quote"] = fields["quote"].strip().strip('"“”')
        if addressee not in PERSONAS or not ask:
            if addressee:
                log(f"dropped ask for unknown addressee {addressee!r}")
            continue
        out.append({"addressee": addressee, "ask": ask, "quote": flat(fields["quote"], QUOTE_MAX)})
    return out


def _fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def extract_asks(memo: dict, body: str) -> list[dict]:
    """One Sonnet call. Returns [] on any failure — extraction must never block the memo."""
    prompt = PROMPT_TEMPLATE.format(
        title=memo.get("title", ""),
        when=memo["recorded"].strftime("%A %-I:%M %p"),
        dur=_fmt_duration(memo.get("duration", 0.0)),
        roster=_roster(),
        transcript=body,
    )
    cmd = [CLAUDE_BIN, "-p", "--permission-mode", "bypassPermissions",
           "--model", EXTRACT_MODEL, "--max-budget-usd", "0.40", prompt]
    try:
        proc = subprocess.run(cmd, cwd=str(VAULT), capture_output=True, text=True, timeout=240)
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"extract error: {e}")
        return []
    if proc.returncode != 0:
        log(f"extract exit {proc.returncode} stderr={proc.stderr[:160]!r}")
        return []
    return parse_asks(proc.stdout)


# ---------------------------------------------------------------------------- #
# Rendering
# ---------------------------------------------------------------------------- #
def _memo_ref(memo: dict, title: str, date: str) -> str:
    return f"\"{flat(title, 80)}\", {date} {memo['recorded']:%-I:%M %p}, Craft daily note {date}"


def task_markdown(ask: dict) -> str:
    """The checkbox line on the daily note. Kit's carries #kit (kit-watch dispatches on it);
    a persona's carries "→ Name" and the persona tags #review when done."""
    p = PERSONAS[ask["addressee"]]
    text = flat(ask["ask"], ASK_MAX)
    quote = flat(ask.get("quote"), QUOTE_MAX)
    line = f"{text} — \"{quote}\"" if quote else text
    return f"{line} #kit" if ask["addressee"] == "kit" else f"{line} → {p['name']}"


def inbox_entry(ask: dict, memo: dict, title: str, block_id: str | None, *, date: str) -> str:
    p = PERSONAS[ask["addressee"]]
    block = block_id or "(not written)"
    return (
        f"### {datetime.now():%Y-%m-%d %H:%M} — voice-memo-dispatch\n\n"
        f"Alex addressed you in a voice memo ({_memo_ref(memo, title, date)}).\n"
        f"Ask: {flat(ask['ask'], ASK_MAX)}\n"
        f"His words: \"{flat(ask.get('quote'), QUOTE_MAX)}\"\n"
        f"A headless {p['name']} run was launched for this at the same time. The checkbox on his "
        f"Craft daily note is block `{block}` — answer under it (`craft append {block} --stdin`) and "
        f"tag it `#review` (`craft tag {block} review`) when done. If the headless run already did, "
        f"clear this entry.\n"
    )


def persona_brief(ask: dict, memo: dict, title: str, block_id: str | None, *, date: str) -> str:
    p = PERSONAS[ask["addressee"]]
    block = block_id or "(none — the Craft write failed; answer into your own inbox file instead)"
    sealed = (
        "\nYour domain is SEALED. Write only inside your own folder and under the Craft block on "
        "his daily note. Nothing from this ask goes into the shared activity log, a Linear issue, "
        "or any other persona's files.\n" if p.get("sealed") else ""
    )
    return f"""You are {p['name']}, running UNATTENDED and headless because Alex addressed you by name in a \
voice memo ({_memo_ref(memo, title, date)}). Your folder's CLAUDE.md boot rules apply; your inbox file \
has a matching entry from voice-memo-dispatch — treat this prompt as that entry, handled now.

THE ASK: {flat(ask['ask'], ASK_MAX)}
HIS WORDS: "{flat(ask.get('quote'), QUOTE_MAX)}"
The full cleaned transcript is inside the "Voice memo: {flat(title, 80)}" toggle on his Craft daily \
note for {date} (`craft search` / the vault mirror at Craft/Daily Notes/{date[:4]}/) — read it \
for context before acting.
{sealed}
DO THE WORK, then respond where he will see it:
- PRIMARY: write your result as children of the checkbox block `{block}` with \
`craft append {block} --stdin` (real markdown; tables for anything with 2+ attributes; \
`###` for sections, never `#`/`##`). Then `craft tag {block} review` — #review is how Alex finds \
finished work. Never remove #review yourself. If you must MENTION a tag, wrap it in backticks.
- Be proportionate: a small ask gets a small answer. A draft is a draft — leave it unsent.

ACT vs ASK (no live human to check with):
- JUST DO: answer in-block; read anything; create NEW content (a note, a draft, an unsent email); \
add a single event to one of Alex's calendars.
- PROPOSE ONLY (write the plan in-block, then `kit-notify "<plain text>"` and STOP): anything sent \
outward to a third party, bookings, purchases, money, editing or deleting things that already \
exist, bulk calendar changes, system or config changes, reaching out to people.
- NEVER: financial transactions, legal or medical acts, mass sends, mass deletion.
- Text Alex via `kit-notify` ONLY if blocked, time-sensitive, or you need a decision. Plain text, \
no markdown, no emojis. Otherwise stay in-block.

HARD LIMITS: no GUI, no launching apps, no installs, no daemon or launchd changes, no git commits, \
no Linear writes, never call advisor(). Browser automation and interactive auth are unavailable."""


def sms_text(title: str, memo: dict, found: list[dict]) -> str:
    """Plain SMS, ≤ 320 chars: one clause per addressee, sealed personas by count only."""
    n = len(found)
    head = f"Heard {n} ask{'s' if n != 1 else ''} in \"{flat(title, 60)}\" ({memo['recorded']:%-I:%M %p}): "
    parts = []
    for a in found:
        p = PERSONAS[a["addressee"]]
        if p.get("sealed"):
            parts.append(f"{p['name']}: one for her")
        else:
            parts.append(f"{p['name']}: {flat(a['ask'], 90)}")
    tail = " Running." if n == 1 else " All running."
    text = head + "; ".join(parts) + "." + tail
    if len(text) > SMS_MAX:
        text = text[: SMS_MAX - 1].rstrip() + "…"
    return text


def summary_line(title: str, found: list[dict]) -> str:
    """The shared activity-log summary — addressee counts, no ask text."""
    counts: dict[str, int] = {}
    for a in found:
        counts[PERSONAS[a["addressee"]]["name"]] = counts.get(PERSONAS[a["addressee"]]["name"], 0) + 1
    who = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in counts.items())
    return flat(f"Dispatched {len(found)} ask(s) from \"{title}\": {who}")


# ---------------------------------------------------------------------------- #
# Side effects — every one of these is monkeypatched away in the tests
# ---------------------------------------------------------------------------- #
def _craft_post_tasks(date: str, lines: list[str]) -> list[str | None]:
    """Top-level checkboxes at the end of the daily note, right after the memo toggle."""
    import craft_write  # local import: keeps this module importable without Craft creds
    base, cred = craft_write.creds()
    blocks = [{"type": "text", "markdown": ln, "listStyle": "task", "indentationLevel": 0}
              for ln in lines]
    created = craft_write._req("POST", f"{base}/blocks", cred,
                               json_body={"blocks": blocks,
                                          "position": {"date": date, "position": "end"}})
    ids = [b.get("id") for b in created.get("items", [])]
    return ids + [None] * (len(lines) - len(ids))


def _vault_append(path: Path, text: str, init_with: str | None = None) -> None:
    cmd = [VAULT_APPEND, str(path), "--stdin"]
    if init_with:
        cmd += ["--init-with", init_with]
    subprocess.run(cmd, input=text, text=True, check=True, timeout=30)


def _spawn_persona(agent: str, cwd: Path, brief: str) -> bool:
    env = dict(os.environ, PATH=PERSONA_PATH, HOME=str(HOME))
    DISPATCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DISPATCH_LOG, "a") as out:
        out.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} → {agent} =====\n")
        subprocess.Popen(
            [CLAUDE_BIN, "--agent", agent, "-p", brief, "--permission-mode", "auto",
             "--max-budget-usd", PERSONA_BUDGET_USD],
            cwd=str(cwd), env=env, stdout=out, stderr=out, start_new_session=True,
        )
    return True


def _inbox_init(p: dict) -> str:
    return (f"---\ncreated: '[[{datetime.now():%Y-%m-%d}]]'\ntags:\n  - inbox\n---\n"
            f"# Inbox: {p['folder']}\n\nDrop point for anything {p['name']} should pick up. "
            f"Format: `### YYYY-MM-DD HH:MM — {{from-agent or session}}` followed by the item.\n\n")


# ---------------------------------------------------------------------------- #
# Orchestration
# ---------------------------------------------------------------------------- #
def run(memo: dict, body: str, title: str, toggle_id: str | None, prev: dict, *,
        date: str, dry_run: bool, notify_enabled: bool, send_sms) -> dict:
    """Extract and route. Returns the ledger fields to merge ({} when nothing to do).
    Never raises."""
    if prev.get("asks_dispatched"):
        return {}
    try:
        found = extract_asks(memo, body)
    except Exception as e:  # noqa: BLE001
        log(f"extract raised: {e}")
        found = []

    plan = []
    for a in found:
        p = PERSONAS[a["addressee"]]
        item = {"addressee": a["addressee"], "ask": a["ask"], "task": task_markdown(a)}
        if a["addressee"] != "kit":
            item["inbox"] = str(CLAUDE_DIR / p["folder"] / "Inboxes" / p["inbox"])
            item["agent"] = a["addressee"]
            item["cwd"] = str(CLAUDE_DIR / p["folder"])
        plan.append(item)

    if dry_run:
        for item in plan:
            route = "kit-watch" if item["addressee"] == "kit" else f"inbox + headless {item['agent']}"
            log(f"[dry-run] {item['addressee']}: {item['ask']!r} → {route}")
        if not plan:
            log("[dry-run] no asks")
        else:
            log(f"[dry-run] WOULD text: {sms_text(title, memo, found)!r}")
        return {"asks": [{"addressee": a["addressee"], "ask": a["ask"]} for a in found],
                "asks_dispatched": False, "asks_notified": False, "plan": plan}

    if not plan:
        return {"asks": [], "asks_dispatched": True, "asks_notified": False}

    # 1) checkboxes on the daily note — one POST, ids come back in order
    block_ids: list[str | None] = [None] * len(plan)
    try:
        block_ids = _craft_post_tasks(date, [item["task"] for item in plan])
    except Exception as e:  # noqa: BLE001
        log(f"craft task write failed: {e}")
    # 2) persona inbox + headless run
    for item, a, bid in zip(plan, found, block_ids):
        if item["addressee"] == "kit":
            continue
        p = PERSONAS[item["addressee"]]
        try:
            _vault_append(Path(item["inbox"]), inbox_entry(a, memo, title, bid, date=date),
                          init_with=_inbox_init(p))
        except Exception as e:  # noqa: BLE001
            log(f"inbox append failed for {p['name']}: {e}")
        try:
            _spawn_persona(item["agent"], Path(item["cwd"]),
                           persona_brief(a, memo, title, bid, date=date))
        except Exception as e:  # noqa: BLE001
            log(f"spawn failed for {p['name']}: {e}")
    # 3) one text, deduped by the caller's ledger via asks_notified
    notified = False
    if notify_enabled:
        try:
            notified = bool(send_sms(sms_text(title, memo, found)))
        except Exception as e:  # noqa: BLE001
            log(f"sms failed: {e}")
    else:
        log(f"WOULD text: {sms_text(title, memo, found)!r}")
    log(summary_line(title, found))
    return {"asks": [{"addressee": a["addressee"], "ask": a["ask"], "block_id": bid}
                     for a, bid in zip(found, block_ids)],
            "asks_dispatched": True, "asks_notified": notified}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Extract asks from a saved transcript (no writes).")
    ap.add_argument("--file", required=True, help="cleaned transcript text")
    ap.add_argument("--title", default="(saved transcript)")
    args = ap.parse_args(argv)
    body = Path(args.file).read_text()
    memo = {"title": args.title, "recorded": datetime.now(), "duration": 0.0}
    found = extract_asks(memo, body)
    if not found:
        print("NONE")
        return 0
    for a in found:
        print(f"{a['addressee']:7} | {a['ask']}\n        | \"{a['quote']}\"")
    print("SMS:", sms_text(args.title, memo, found))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
