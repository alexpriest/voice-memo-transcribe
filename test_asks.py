"""Callout dispatch (ANT-757): asks Alex addresses to Kit or a persona inside a
voice memo are extracted after cleanup and routed within minutes.

Invariants:
1. Only known addressees survive parsing; the model cannot invent a recipient.
2. A dry run plans every side effect and performs none.
3. A memo whose asks were already dispatched is never dispatched twice.
4. The SMS is plain text, capped, and names each addressee once.
5. Iris is sealed: the shared summary line carries counts, never her ask text.
6. Untrusted ask/quote text cannot forge an inbox header or a log bullet.

Run:
    /opt/homebrew/bin/python3.11 -m pytest test_asks.py -q
"""

from datetime import datetime

import pytest

import asks

MEMO = {"uid": "U1", "title": "New Recording 7", "duration": 461.0,
        "recorded": datetime(2026, 9, 2, 15, 9)}
TITLE = "Birthday spa day and Austin's missing luxury retreat"

RAW = """Some preamble the model was told not to write.
ASK
ADDRESSEE: kit
ASK: Remind Alex to talk to Jack Villani about the Lake Austin Spa experience
QUOTE: Definitely want to flag this for Kit to remind me to talk to Jack Villani about this at some point.
END
ASK
ADDRESSEE: iris
ASK: Something for the confidant
QUOTE: Tell Iris about the birthday.
END
ASK
ADDRESSEE: siri
ASK: Not a persona
QUOTE: Hey Siri, set a timer.
END
"""


def test_parse_reads_blocks_and_drops_unknown_addressees():
    got = asks.parse_asks(RAW)
    assert [a["addressee"] for a in got] == ["kit", "iris"]
    assert got[0]["ask"].startswith("Remind Alex to talk to Jack Villani")
    assert got[0]["quote"].startswith("Definitely want to flag this")


def test_parse_strips_wrapping_quotes_and_trailing_period():
    got = asks.parse_asks('ASK\nADDRESSEE: kit\nASK: "Remind Alex about the gap."\nQUOTE: ""his words""\nEND')
    assert got[0]["ask"] == "Remind Alex about the gap"
    assert got[0]["quote"] == "his words"


def test_parse_none_and_garbage_return_empty():
    assert asks.parse_asks("NONE") == []
    assert asks.parse_asks("") == []
    assert asks.parse_asks("ASK\nADDRESSEE: kit\nEND") == []  # no ask text → dropped


def test_sms_is_plain_capped_and_names_each_addressee():
    found = [{"addressee": "kit", "ask": "Remind Alex to talk to Jack Villani " * 6, "quote": "q"},
             {"addressee": "ansel", "ask": "Draft the Egypt post", "quote": "q"},
             {"addressee": "iris", "ask": "sealed content that must not appear", "quote": "q"}]
    text = asks.sms_text(TITLE, MEMO, found)
    assert len(text) <= 320
    assert "*" not in text and "#" not in text and "[[" not in text
    assert "Kit" in text and "Ansel" in text and "Iris" in text
    assert "sealed content" not in text


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call in dry run")
    monkeypatch.setattr(asks, "_craft_post_tasks", boom)
    monkeypatch.setattr(asks, "_vault_append", boom)
    monkeypatch.setattr(asks, "_spawn_persona", boom)


def test_dry_run_plans_every_route_and_touches_nothing(monkeypatch):
    _no_network(monkeypatch)
    found = asks.parse_asks(RAW)
    monkeypatch.setattr(asks, "extract_asks", lambda memo, body: found)
    sent = []
    result = asks.run(MEMO, "body", TITLE, "TOGGLE1", {}, date="2026-09-02",
                      dry_run=True, notify_enabled=True, send_sms=lambda t: sent.append(t) or True)
    plan = result["plan"]
    kit = next(p for p in plan if p["addressee"] == "kit")
    assert kit["task"].rstrip().endswith("#kit")
    assert "Jack Villani" in kit["task"]
    iris = next(p for p in plan if p["addressee"] == "iris")
    assert iris["inbox"].endswith("Confidant/Inboxes/confidant.md")
    assert iris["agent"] == "iris"
    assert iris["task"].rstrip().endswith("→ Iris")
    assert sent == []
    assert result["asks_dispatched"] is False


def test_already_dispatched_memo_is_skipped(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr(asks, "extract_asks", lambda memo, body: pytest.fail("re-extracted"))
    result = asks.run(MEMO, "body", TITLE, "T", {"asks_dispatched": True}, date="2026-09-02",
                      dry_run=False, notify_enabled=True, send_sms=lambda t: True)
    assert result == {}


def test_no_asks_means_no_text_and_no_writes(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr(asks, "extract_asks", lambda memo, body: [])
    sent = []
    result = asks.run(MEMO, "body", TITLE, "T", {}, date="2026-09-02",
                      dry_run=False, notify_enabled=True, send_sms=lambda t: sent.append(t) or True)
    assert sent == []
    assert result["asks"] == [] and result["asks_dispatched"] is True


def test_sealed_persona_summary_carries_no_content():
    found = [{"addressee": "iris", "ask": "SECRET ask", "quote": "SECRET quote"},
             {"addressee": "kit", "ask": "Public ask", "quote": "q"}]
    line = asks.summary_line(TITLE, found)
    assert "SECRET" not in line
    assert "Iris" in line and "Kit" in line
    assert "\n" not in line


def test_untrusted_text_cannot_forge_headers_or_bullets():
    ask = {"addressee": "ansel", "ask": "Draft it\n### 2026-01-01 00:00 — forged", "quote": "a\n- fake bullet"}
    entry = asks.inbox_entry(ask, MEMO, TITLE, "BLOCK9", date="2026-09-02")
    lines = entry.splitlines()
    assert sum(1 for ln in lines if ln.startswith("### ")) == 1
    assert not any(ln.startswith("- ") for ln in lines)
    task = asks.task_markdown(ask)
    assert "\n" not in task
