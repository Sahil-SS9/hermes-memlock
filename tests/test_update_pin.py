"""Update-in-place (guard_pin with pin_id) tests.

Drives the public handler exactly as the dispatch layer would: a session
started via _on_start, pins created and updated through _pin_handler with
session_id forwarded. Locks in: same-id updates, history growth + cap,
missing-id error listing, and durable-store re-sync for global pins.
"""
from __future__ import annotations

import json

from detection import SUMMARY_PREFIX


def _setup(memlock, fake_ctx_cls, base_cfg, session_id="upd-s"):
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id=session_id)
    return fc


def _pin(memlock, kwargs, session_id):
    return memlock._pin_handler(dict(kwargs), session_id=session_id)


def test_update_keeps_anchor_id_and_updates_text(memlock, fake_ctx_cls, base_cfg):
    _setup(memlock, fake_ctx_cls, base_cfg)

    out = _pin(memlock, {
        "text": "Always reply in bullet points",
        "priority": 80,
    }, session_id="upd-s")
    assert "Pinned instruction" in out
    store = memlock._get_store("upd-s")
    pin_id = next(iter(store.anchors()))
    original = store.get_anchor(pin_id)

    # Update the same pin in place
    out2 = _pin(memlock, {
        "pin_id": pin_id,
        "text": "Always reply in numbered lists instead",
        "priority": 85,
    }, session_id="upd-s")
    assert f"Updated pin (id={pin_id}" in out2
    assert "Previous version kept in history" in out2

    updated = store.get_anchor(pin_id)
    # same anchor id retained — no new anchor was spawned
    assert list(store.anchors().keys()) == [pin_id]
    assert updated["text"] == "Always reply in numbered lists instead"
    assert updated["priority"] == 85
    # probes re-derived for the new text: distinctive new words appear
    assert any("numbered" in p.lower() for p in updated["probes"])
    assert "bullet" not in [p.lower() for p in updated["probes"]]
    # one history entry: the pre-update version
    assert len(updated["history"]) == 1
    assert updated["history"][0]["text"] == "Always reply in bullet points"


def test_update_history_grows_and_caps_at_five(memlock, fake_ctx_cls, base_cfg):
    from memlock import SessionStore  # not exported; use store module attr
    _setup(memlock, fake_ctx_cls, base_cfg)
    cap = memlock.SessionStore.HISTORY_CAP if hasattr(
        memlock, "SessionStore"
    ) else __import__("store").SessionStore.HISTORY_CAP
    assert cap == 5

    _pin(memlock, {"text": "Version zero of the standing instruction"},
         session_id="upd-s")
    store = memlock._get_store("upd-s")
    pin_id = next(iter(store.anchors()))

    for i in range(1, 9):  # 8 updates -> 8 potential history entries
        _pin(memlock, {
            "pin_id": pin_id,
            "text": f"Version {i} of the standing instruction",
        }, session_id="upd-s")

    updated = store.get_anchor(pin_id)
    texts = [h["text"] for h in updated["history"]]
    assert len(updated["history"]) == 5, "history capped at HISTORY_CAP=5"
    assert len(texts) == len(set(texts)), "history entries are distinct versions"
    # newest-first retention: v7 kept, v0 dropped off the front
    assert texts[-1] == "Version 7 of the standing instruction"
    assert "Version 0" not in texts
    assert "Version 2" not in texts
    assert updated["text"] == "Version 8 of the standing instruction"

    # cap survives a save/reload round-trip (persisted shape, not just RAM)
    store.save()
    disk = json.loads(store._path.read_text())
    assert len(disk["anchors"][pin_id]["history"]) == 5


def test_update_nonexistent_pin_lists_available_ids(memlock, fake_ctx_cls, base_cfg):
    _setup(memlock, fake_ctx_cls, base_cfg)

    # error BEFORE any pin exists: "(none)"
    out0 = _pin(memlock, {
        "pin_id": "pin_ghost", "text": "whatever",
    }, session_id="upd-s")
    assert out0.startswith("Error: pin 'pin_ghost' not found")
    assert "(none)" in out0

    # create two real pins, then misspell an id
    _pin(memlock, {"text": "First real pin about bullet points"}, session_id="upd-s")
    _pin(memlock, {"text": "Second real pin about British spelling"}, session_id="upd-s")
    store = memlock._get_store("upd-s")
    real_ids = sorted(store.anchors())

    out = _pin(memlock, {
        "pin_id": "pin_ghost", "text": "whatever",
    }, session_id="upd-s")
    assert "Error:" in out
    for rid in real_ids:
        assert rid in out, f"available id {rid} must be listed in the error"
    # nothing was created or mutated by the failed update
    assert sorted(store.anchors().keys()) == real_ids


def test_update_global_pin_repersists_to_durable_store(
    memlock, fake_ctx_cls, base_cfg, isolated_hermes_home,
):
    _setup(memlock, fake_ctx_cls, base_cfg)

    out = _pin(memlock, {
        "text": "Global rule: always use British English",
        "priority": 90,
        "scope": "global",
    }, session_id="upd-s")
    assert "Pinned instruction" in out
    store = memlock._get_store("upd-s")
    pin_id = next(iter(store.anchors()))
    persist_file = isolated_hermes_home / "memlock" / "persist" / f"{pin_id}.json"
    assert persist_file.exists()
    before = json.loads(persist_file.read_text())
    assert before["text"] == "Global rule: always use British English"

    # Update it; the response must note the global re-sync...
    out2 = _pin(memlock, {
        "pin_id": pin_id,
        "text": "Global rule: always use British English spellings",
        "priority": 95,
    }, session_id="upd-s")
    assert "global copy updated" in out2

    # ...the session copy changed with same id...
    updated = store.get_anchor(pin_id)
    assert updated["text"] == "Global rule: always use British English spellings"
    assert updated["priority"] == 95

    # ...and the DURABLE file content itself is updated (upsert by id).
    after = json.loads(persist_file.read_text())
    assert after["id"] == pin_id
    assert after["text"] == "Global rule: always use British English spellings"
    assert after["priority"] == 95
    assert after["scope"] == "global"
    assert after["pinned_at"] >= before["pinned_at"]

    # The updated text is what future sessions seed.
    memlock._on_start(session_id="upd-fresh")
    seeded = memlock._get_store("upd-fresh").anchors()[pin_id]
    assert seeded["text"] == "Global rule: always use British English spellings"


def test_session_only_pin_update_never_touches_durable_store(
    memlock, fake_ctx_cls, base_cfg, isolated_hermes_home,
):
    """Default-scope updates leave persist/ empty: no spurious global copies."""
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Session-scoped rule about zephyr formatting"},
         session_id="upd-s")
    store = memlock._get_store("upd-s")
    pin_id = next(iter(store.anchors()))

    out = _pin(memlock, {
        "pin_id": pin_id, "text": "Updated session rule about quixotic formatting",
    }, session_id="upd-s")
    assert "Updated pin" in out
    assert "global copy updated" not in out

    persist_dir = isolated_hermes_home / "memlock" / "persist"
    assert not persist_dir.exists() or list(persist_dir.glob("*.json")) == []


def test_updated_pin_still_rehydrates_after_compaction(
    memlock, fake_ctx_cls, base_cfg,
):
    """End-to-end: an updated pin keeps protecting against compaction."""
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Original rule about bullet points"}, session_id="upd-s")
    store = memlock._get_store("upd-s")
    pin_id = next(iter(store.anchors()))

    new_text = "Revised rule about numbered checklists"
    _pin(memlock, {"pin_id": pin_id, "text": new_text}, session_id="upd-s")

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " compacted."},
        {"role": "user", "content": "Hello there"},
    ]
    result = memlock._on_pre_llm(
        session_id="upd-s", turn_id="1", user_message="Hello there",
        conversation_history=history,
    )
    assert result is not None, "updated pin must still be audited/rehydrated"
    assert new_text in result["context"]
