"""No test may reach the network. The callout pass (asks.py) calls Claude, Craft, vault-append
and spawns persona runs; every one is stubbed here by default. A test that wants a specific
extraction result monkeypatches asks.extract_asks itself (test-level patches win)."""
import pytest

import asks


@pytest.fixture(autouse=True)
def _no_callout_side_effects(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("callout side effect reached from a test")
    monkeypatch.setattr(asks, "extract_asks", lambda memo, body: [])
    monkeypatch.setattr(asks, "_craft_post_tasks", boom)
    monkeypatch.setattr(asks, "_vault_append", boom)
    monkeypatch.setattr(asks, "_spawn_persona", boom)
