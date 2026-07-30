"""Write a voice memo into a Craft daily note as a collapsible toggle.

Structure produced (matches the format Alex specced):

    ───────────────────────────────────                 (divider; keeps the memo
    ▸ ### Voice memo: <title> · <time> · <duration>      from running into the
         <transcript paragraph 1>                        rest of the daily note.
                                                         ONE per note — see
                                                         _needs_divider)
         <transcript paragraph 2>
         *Flagged: …*                                    (only if uncertainties)
         🎵 <pretty-name>.m4a                             (nested, plays)

Craft API facts (reverse-engineered — see craft-mirror/README.md):
  * POST /blocks {blocks, position:{date|pageId|siblingId, position}} creates blocks.
    Nesting is by `indentationLevel` (children = parent + 1). listStyle "toggle"
    makes the collapsible header.
  * A divider is `{type:"text", markdown:"---"}` — it comes back as `type:"line"`.
    There is no `{type:"divider"}`; that 400s with a union-validation error.
  * POST /upload?<position> with the RAW file bytes (Content-Type: audio/mp4) hosts
    the audio and returns {blockId, assetUrl}. It cannot set a fileName or indent.
  * To get a NAMED, NESTED audio block, upload for the bytes, then create a
    `{type:file, url:<assetUrl>, fileName, indentationLevel}` block via /blocks and
    delete the loose upload block.

Creds come from the environment (CRAFT_BASE_URL / CRAFT_CREDENTIAL), a local .env,
or `op` as a last resort — never `op` under launchd (it stalls on the desktop app).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ENV_FILE = Path(__file__).resolve().parent / ".env"

# Gateway statuses worth replaying — the request never reached the app, so a POST is safe to
# repeat. 500 is deliberately excluded: it may have partially applied.
_RETRY_STATUS = {429, 502, 503, 504}
_REQ_ATTEMPTS = 4
_REQ_BACKOFF_S = 2.0


class CraftError(Exception):
    pass


def _load_env_file():
    if not _ENV_FILE.is_file():
        return
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line[len("export "):] if line.startswith("export ") else line
        if "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _op(field: str) -> str:
    key = {"base_url": "CRAFT_BASE_URL", "credential": "CRAFT_CREDENTIAL"}[field]
    if os.environ.get(key):
        return os.environ[key].strip()
    a = ["op", "item", "get", "Craft API", "--vault", "Claude", "--fields", f"label={field}"]
    if field == "credential":
        a.append("--reveal")
    return subprocess.run(a, capture_output=True, text=True, check=True, timeout=20).stdout.strip()


def creds() -> tuple[str, str]:
    _load_env_file()
    base = _op("base_url").rstrip("/")
    cred = _op("credential")
    if not base or not cred:
        raise CraftError("missing Craft credentials (CRAFT_BASE_URL / CRAFT_CREDENTIAL)")
    return base, cred


def _req(method: str, url: str, cred: str, *, json_body=None, raw_body=None, content_type=None):
    headers = {"Authorization": f"Bearer {cred}", "User-Agent": _UA}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers["Content-Type"] = content_type or "application/octet-stream"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # Craft's edge returns transient 502/503/504 (hit 2026-07-30, stranding a memo that had
    # already cost a full transcribe+enrich). Retry gateway-level failures only: those mean the
    # request did not reach the app, so replaying a non-idempotent POST won't duplicate a block.
    # A bare 500 is NOT retried — that one may have partially applied.
    last = None
    for attempt in range(1, _REQ_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8")
                return json.loads(body) if body.strip() else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            last = CraftError(f"{method} {url.split('?')[0]} -> {e.code}: {detail}")
            if e.code not in _RETRY_STATUS:
                raise last
        except (urllib.error.URLError, TimeoutError) as e:
            last = CraftError(f"{method} {url.split('?')[0]} -> network error: {e}")
        if attempt < _REQ_ATTEMPTS:
            time.sleep(_REQ_BACKOFF_S * (2 ** (attempt - 1)))
    raise last


# --------------------------------------------------------------------------- #

def _blocks(base, cred, date):
    """Fetch a daily note's block tree (top-level content list)."""
    d = _req("GET", f"{base}/blocks?date={date}", cred)
    return d.get("content", []) if isinstance(d, dict) else []


def _needs_divider(content) -> bool:
    """Should this memo lay down a divider? One per note, not one per write.

    Same rule as craft_append.needs_separator (craft-mirror) — keep them in step.
    Test what the write will land against, not the note as a whole: nothing there,
    an existing divider, or a task means no rule; prose means yes. Alex flagged
    stacked `***` as noise on 2026-07-29, and he separates his own timestamped
    journal entries with `***`, so "note already contains a divider" is the wrong
    test — it would suppress the one rule genuinely needed on a journaled day.
    """
    for block in reversed(content or []):
        if not (block.get("markdown") or "").strip():
            continue
        if block.get("type") == "line":
            return False
        return block.get("listStyle") != "task"
    return False


def _find_existing_group(blocks, toggle_id) -> list[str]:
    """Return block ids of a previously-written voice-memo group, for replacement.

    Identity comes from the toggle's block id (recorded in the local ledger), NOT
    from a marker in the visible text — Alex reads these notes and a stray
    `^vm-1A2B3C4D` in the header is noise. The group is the toggle header, the run
    of blocks indented beneath it, and the divider immediately preceding it.

    Returns [] if the toggle is gone (he deleted or moved it) — we then just append
    a fresh group rather than guessing at a match.
    """
    if not toggle_id:
        return []
    ids = []
    n = len(blocks)
    for i, b in enumerate(blocks):
        if b.get("id") != toggle_id:
            continue
        if i and blocks[i - 1].get("type") == "line":
            ids.append(blocks[i - 1]["id"])
        ids.append(b["id"])
        j = i + 1
        while j < n and (blocks[j].get("indentationLevel") or 0) >= 1:
            ids.append(blocks[j]["id"])
            j += 1
        break
    return ids


# Craft rejects any single block over 20,000 characters with a 400. Stay well
# under it: the cost of an extra block is invisible, the cost of a 400 is the
# whole run.
MAX_BLOCK_CHARS = 15_000

# Craft rejects uploads over 5MB. Kept just under so a boundary case doesn't 400.
AUDIO_UPLOAD_LIMIT = 4_900_000


def _split_oversized(text: str, limit: int = MAX_BLOCK_CHARS) -> list[str]:
    """Break one over-long paragraph on the nicest boundary available."""
    out = []
    while len(text) > limit:
        window = text[:limit]
        # Prefer a sentence end, then any whitespace, then a hard cut.
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "))
        cut = cut + 1 if cut > limit // 2 else -1
        if cut == -1:
            ws = window.rfind(" ")
            cut = ws if ws > limit // 2 else limit
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def _paragraphs_for_craft(transcript: str) -> list[str]:
    """Transcript -> Craft-safe paragraph blocks.

    Two hazards, both hit in production on a 27-minute memo (2026-07-25):

    1. The cleaned transcript is separated by blank lines, but when enrichment
       fails the caller falls back to the RAW transcript, which is separated by
       single newlines. Splitting only on "\\n\\n" then yields ONE paragraph
       containing the entire memo.
    2. Craft 400s on any block over 20,000 characters. That 400 propagated out
       of the run, so the memo was never marked done and every later trigger
       re-transcribed and re-failed it forever, blocking every memo behind it.

    So: prefer blank-line paragraphs, fall back to single newlines when that
    yields one huge blob, then hard-split anything still over the limit.
    """
    text = (transcript or "").strip()
    if not text:
        return ["(no transcript)"]

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) <= 1 and len(text) > MAX_BLOCK_CHARS:
        paras = [p.strip() for p in text.split("\n") if p.strip()] or [text]

    out: list[str] = []
    for p in paras:
        out.extend(_split_oversized(p) if len(p) > MAX_BLOCK_CHARS else [p])
    return out or ["(no transcript)"]


def write_voice_memo(*, date: str, title: str, time_label: str, duration_label: str,
                     transcript: str, uncertainties, audio_path: str, pretty_name: str,
                     shortid: str, prev_toggle_id: str | None = None) -> dict:
    """Create (or replace) the voice-memo toggle on the given Craft daily note.

    Pass `prev_toggle_id` (from the ledger) when reprocessing a memo so the old
    group is replaced instead of duplicated.

    Returns {"toggle_id", "audio_block_id"}.
    """
    base, cred = creds()

    # Replace any prior group for this memo (idempotent reprocess).
    content = _blocks(base, cred, date)
    old = _find_existing_group(content, prev_toggle_id)
    if old:
        _req("DELETE", f"{base}/blocks", cred, json_body={"blockIds": old})
        content = [b for b in content if b.get("id") not in set(old)]

    # 1) divider (only if the memo would otherwise run into Alex's own writing —
    #    see _needs_divider), then toggle header + transcript paragraphs (+ flags)
    #    at indent 1.
    divider = _needs_divider(content)
    header = f"### Voice memo: {title} · {time_label} · {duration_label}"
    blocks = ([{"type": "text", "markdown": "---", "indentationLevel": 0}] if divider else []) + [
        {"type": "text", "markdown": header, "listStyle": "toggle", "indentationLevel": 0}]
    paragraphs = _paragraphs_for_craft(transcript)
    for p in paragraphs:
        blocks.append({"type": "text", "markdown": p, "indentationLevel": 1})
    if uncertainties:
        blocks.append({"type": "text",
                       "markdown": f"*Flagged: {'; '.join(uncertainties)}*",
                       "indentationLevel": 1})
    created = _req("POST", f"{base}/blocks", cred,
                   json_body={"blocks": blocks, "position": {"date": date, "position": "end"}})
    items = created.get("items", [])
    toggle_index = 1 if divider else 0  # items[0] is the divider when there is one
    if len(items) <= toggle_index:
        raise CraftError(f"expected a toggle at index {toggle_index}, got {len(items)} block(s)")
    toggle_id = items[toggle_index]["id"]
    last_text_id = items[-1]["id"]

    # 2) attach the audio. If this fails the transcript is already on the page, so the
    #    toggle id must reach the caller — otherwise the ledger records an error with no
    #    toggle, _find_existing_group can't match it, and a retry appends a DUPLICATE
    #    group. (2026-07-30: a 502 here left an orphan top-level upload block behind.)
    try:
        audio_block_id = _attach_audio(base, cred, audio_path, pretty_name, last_text_id)
    except Exception as e:  # noqa: BLE001 - any failure here must still carry the toggle
        e.toggle_id = toggle_id
        raise
    return {"toggle_id": toggle_id, "audio_block_id": audio_block_id}


def _attach_audio(base, cred, audio_path: str, pretty_name: str,
                  last_text_id: str) -> str | None:
    """Upload the audio (loose block), re-attach it as a named block nested under the
    toggle after the transcript, and delete the loose upload block.

    Craft caps uploads at 5MB, which a long memo exceeds (a 27-minute one is ~5.3MB).
    The transcript is the artifact worth keeping and it is already written by this
    point, so an oversized attachment must not throw the whole memo away — the audio
    still exists in Voice Memos either way. Say so in the note rather than leaving a
    silent gap where an attachment should be.
    """
    audio_bytes = Path(audio_path).read_bytes()
    if len(audio_bytes) > AUDIO_UPLOAD_LIMIT:
        mb = len(audio_bytes) / 1_000_000
        _req("POST", f"{base}/blocks", cred, json_body={
            "blocks": [{"type": "text",
                        "markdown": f"*Audio not attached — {mb:.1f}MB exceeds Craft's 5MB limit. "
                                    f"The recording is in Voice Memos.*",
                        "indentationLevel": 1}],
            "position": {"siblingId": last_text_id, "position": "after"},
        })
        return None

    up = _req("POST", f"{base}/upload?siblingId={last_text_id}&position=after",
              cred, raw_body=audio_bytes, content_type="audio/mp4")
    asset_url = up.get("assetUrl")
    loose_id = up.get("blockId")
    if not asset_url:
        raise CraftError(f"upload returned no assetUrl: {up}")
    file_block = _req("POST", f"{base}/blocks", cred, json_body={
        "blocks": [{"type": "file", "url": asset_url, "fileName": pretty_name,
                    "markdown": f"[{pretty_name}]({asset_url})", "indentationLevel": 1}],
        "position": {"siblingId": last_text_id, "position": "after"},
    })
    audio_block_id = file_block.get("items", [{}])[0].get("id")
    if loose_id:
        try:
            _req("DELETE", f"{base}/blocks", cred, json_body={"blockIds": [loose_id]})
        except CraftError:
            pass  # a lingering loose block is cosmetic, not worth failing the memo

    return audio_block_id
