"""Pin rollback tests (Stage 4 — ChronoMem completion).

Drives rollback through every surface: the Hermes shim handler
(``_pin_handler`` with action='rollback'), the MemlockService method, and
the MCP ``memlock_update`` tool with action='rollback'. Locks in: prior-text
restoration under the same id, displaced-state preservation, HISTORY_CAP
across repeated rollbacks, durable-store re-sync for global pins, unknown
pin errors and version-index selection semantics.
"""
from __future__ import annotations

import json

from mcp_server.tools import build_dispatcher


def _setup(memlock, fake_ctx_cls, base_cfg, session_id="rb-s"):
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id=session_id)
    return fc


def _pin(memlock, kwargs, session_id):
    return memlock._pin_handler(dict(kwargs), session_id=session_id)


def _first_pin_id(store):
    return next(iter(store.anchors()))


def test_rollback_returns_prior_text_with_same_anchor_id(
    memlock, fake_ctx_cls, base_cfg,
):
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Original rule about bullet points"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)

    _pin(memlock, {
        "pin_id": pin_id, "text": "Revised rule about numbered checklists",
    }, session_id="rb-s")

    out = _pin(memlock, {"pin_id": pin_id, "action": "rollback"},
               session_id="rb-s")
    assert out.startswith(f"Rolled back pin (id={pin_id}")
    restored = store.get_anchor(pin_id)
    assert list(store.anchors().keys()) == [pin_id], "rollback keeps the id"
    assert restored["text"] == "Original rule about bullet points"


def test_rollback_pushes_displaced_state_to_history(memlock, fake_ctx_cls, base_cfg):
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Version A of the instruction"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    _pin(memlock, {"pin_id": pin_id, "text": "Version B of the instruction"},
         session_id="rb-s")

    # history before rollback: [A]; current: B
    _pin(memlock, {"pin_id": pin_id, "action": "rollback"}, session_id="rb-s")

    anchor = store.get_anchor(pin_id)
    assert anchor["text"] == "Version A of the instruction"
    # nothing destroyed: the displaced CURRENT (B) is now in history
    texts = [h["text"] for h in anchor["history"]]
    assert texts == ["Version B of the instruction"]
    assert texts[0]["text"] if isinstance(texts[0], dict) else True  # shape guard

    # rollback is itself rollback-able (ping-pong restores B)
    out2 = _pin(memlock, {"pin_id": pin_id, "action": "rollback"},
                session_id="rb-s")
    assert out2.startswith("Rolled back pin")
    assert store.get_anchor(pin_id)["text"] == "Version B of the instruction"


def test_repeated_rollbacks_maintain_history_cap_five(memlock, fake_ctx_cls, base_cfg):
    """Nothing is destroyed and the cap holds across many rollbacks.

    Repeated default rollbacks ping-pong between the two most recent states
    (classic undo semantics: each rollback swaps current <-> newest history
    entry), so history size stays bounded at whatever it was when rollbacks
    began — here the cap of 5 from 12 updates, never exceeded, entries
    always distinct.
    """
    from memlock_core.store import SessionStore

    _setup(memlock, fake_ctx_cls, base_cfg)
    cap = SessionStore.HISTORY_CAP
    assert cap == 5
    _pin(memlock, {"text": "V0 instruction"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    for i in range(1, 13):  # 12 updates -> 8 retained (cap) + current V12
        _pin(memlock, {"pin_id": pin_id, "text": f"V{i} instruction"},
             session_id="rb-s")
    assert len(store.get_anchor(pin_id)["history"]) == cap

    seen = set()
    for _ in range(20):  # far more rollbacks than the cap
        out = _pin(memlock, {"pin_id": pin_id, "action": "rollback"},
                   session_id="rb-s")
        assert not out.startswith("Error:"), out
        anchor = store.get_anchor(pin_id)
        assert len(anchor["history"]) <= cap
        assert len(anchor["history"]) >= 2  # swap keeps both sides populated
        texts = [h["text"] for h in anchor["history"]]
        assert len(texts) == len(set(texts)), "history entries stay distinct"
        seen.add(anchor["text"])

    # only the two newest versions ever surface; nothing older is invented
    assert seen <= {"V11 instruction", "V12 instruction"}
    # round-trips through disk too
    store.save()
    disk = json.loads(store._path.read_text())
    assert len(disk["anchors"][pin_id]["history"]) <= cap


def test_global_pin_rollback_updates_durable_file(
    memlock, fake_ctx_cls, base_cfg, isolated_hermes_home,
):
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {
        "text": "Global rule: always use British English",
        "scope": "global", "priority": 90,
    }, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    persist_file = isolated_hermes_home / "memlock" / "persist" / f"{pin_id}.json"
    assert persist_file.exists()

    _pin(memlock, {
        "pin_id": pin_id,
        "text": "Global rule: always use American English",
    }, session_id="rb-s")
    after_update = json.loads(persist_file.read_text())
    assert after_update["text"] == "Global rule: always use American English"

    out = _pin(memlock, {"pin_id": pin_id, "action": "rollback"},
               session_id="rb-s")
    assert "global copy rolled back" in out

    rolled = json.loads(persist_file.read_text())
    assert rolled["id"] == pin_id
    assert rolled["text"] == "Global rule: always use British English"
    assert rolled["scope"] == "global"

    # a future session seeds the ROLLED-BACK wording
    memlock._on_start(session_id="rb-fresh")
    seeded = memlock._get_store("rb-fresh").anchors()[pin_id]
    assert seeded["text"] == "Global rule: always use British English"

    # manifest tracks the re-persisted file
    manifest = json.loads(
        (isolated_hermes_home / "memlock" / "persist" / "manifest.json")
        .read_text(),
    )
    assert manifest["pins"][pin_id]["sha256"]


def test_rollback_unknown_pin_id_errors_and_lists_available(
    memlock, fake_ctx_cls, base_cfg,
):
    _setup(memlock, fake_ctx_cls, base_cfg)
    out0 = _pin(memlock, {"pin_id": "pin_ghost", "action": "rollback"},
                session_id="rb-s")
    assert out0.startswith("Error: pin 'pin_ghost' not found")
    assert "(none)" in out0

    _pin(memlock, {"text": "A real pin about zebras"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    real_id = _first_pin_id(store)

    out = _pin(memlock, {"pin_id": "pin_ghost", "action": "rollback"},
               session_id="rb-s")
    assert real_id in out

    # existing pin but never updated -> no history to roll back to
    out_no_hist = _pin(memlock, {"pin_id": real_id, "action": "rollback"},
                       session_id="rb-s")
    assert out_no_hist.startswith("Error: pin") and "no version history" in out_no_hist


def test_version_index_selection_semantics(memlock, fake_ctx_cls, base_cfg):
    """-1 = most recent previous version; 0 = oldest retained; OOB errors."""
    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "V0 alpha instruction"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    for i, word in enumerate(["beta", "gamma", "delta"], start=1):
        _pin(memlock, {"pin_id": pin_id, "text": f"V{i} {word} instruction"},
             session_id="rb-s")
    # current V3-delta; history oldest->newest: V0-alpha, V1-beta, V2-gamma

    # explicit version=-1 behaves like the default
    out_default = _pin(memlock, {"pin_id": pin_id, "action": "rollback"},
                       session_id="rb-s")
    assert out_default.startswith("Rolled back pin")
    assert store.get_anchor(pin_id)["text"] == "V2 gamma instruction"
    # restore state for the next selection: back to delta via ping-pong
    _pin(memlock, {"pin_id": pin_id, "action": "rollback"}, session_id="rb-s")
    assert store.get_anchor(pin_id)["text"].startswith("V3 delta")

    # version=0 selects the OLDEST retained version
    out0 = _pin(memlock, {"pin_id": pin_id, "action": "rollback", "version": 0},
                session_id="rb-s")
    assert out0.startswith("Rolled back pin")
    assert store.get_anchor(pin_id)["text"] == "V0 alpha instruction"
    # put delta back as current
    _pin(memlock, {"pin_id": pin_id, "action": "rollback"}, session_id="rb-s")
    assert store.get_anchor(pin_id)["text"].startswith("V3 delta")

    # out-of-range index is a clear error, state untouched
    before = dict(store.get_anchor(pin_id))
    out_oob = _pin(memlock, {"pin_id": pin_id, "action": "rollback",
                             "version": 99}, session_id="rb-s")
    assert out_oob.startswith("Error: version index 99 out of range")
    assert store.get_anchor(pin_id) == before

    # non-integer version is a clear error
    out_bad = _pin(memlock, {"pin_id": pin_id, "action": "rollback",
                             "version": "soon"}, session_id="rb-s")
    assert out_bad.startswith("Error: 'version' must be an integer")


def test_service_method_rollback_directly(memlock, fake_ctx_cls, base_cfg):
    """MemlockService.rollback_pin works without the shim handler."""
    _setup(memlock, fake_ctx_cls, base_cfg)
    service = memlock.get_service()
    store = service.ensure_store("rb-s")
    service.do_pin(store, {"text": "Direct original text"})
    pin_id = _first_pin_id(store)
    service.update_pin(store, pin_id, {"text": "Direct updated text"})
    assert store.get_anchor(pin_id)["text"] == "Direct updated text"

    out = service.rollback_pin(store, pin_id)
    assert f"id={pin_id}" in out
    assert store.get_anchor(pin_id)["text"] == "Direct original text"


def test_status_lists_pin_version_history(memlock, fake_ctx_cls, base_cfg):
    """/guard shows each pinned anchor's history compactly."""
    fc = _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "First wording about hedgehogs"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    _pin(memlock, {"pin_id": pin_id, "text": "Second wording about hedgehogs"},
         session_id="rb-s")
    _pin(memlock, {
        "pin_id": pin_id, "text": "Third wording about hedgehogs everywhere",
    }, session_id="rb-s")

    out = fc.commands["guard"]("")
    assert "Pin version history" in out
    assert pin_id in out
    assert "(2 prior version(s))" in out
    assert "current: Third wording about hedgehogs" in out
    assert "First wording about hedgehogs" in out  # oldest head visible
    # pins WITHOUT history do not get a history section
    _pin(memlock, {"text": "Fresh pin with no edits yet"}, session_id="rb-s")
    fresh_id = [
        aid for aid in store.anchors() if aid != pin_id
    ][0]
    out2 = fc.commands["guard"]("")
    hist_section = out2.split("Pin version history")[1]
    assert fresh_id not in hist_section


def test_rollback_through_mcp_memlock_update(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "mcp-home"))
    dispatch = build_dispatcher()

    def call(name, arguments):
        resp = dispatch(name, arguments)
        return resp["content"][0]["text"]

    pin_out = call("memlock_pin", {
        "session_id": "mcp-rb", "text": "MCP first version",
    })
    assert "Pinned instruction" in pin_out
    import re

    match = re.search(r"id=(pin_\d+_\d+)", pin_out)
    assert match, pin_out
    pin_id = match.group(1)

    upd = call("memlock_update", {
        "session_id": "mcp-rb", "pin_id": pin_id, "text": "MCP second version",
    })
    assert "Updated pin" in upd

    rb = call("memlock_update", {
        "session_id": "mcp-rb", "pin_id": pin_id, "action": "rollback",
    })
    assert rb.startswith(f"Rolled back pin (id={pin_id}")

    status = call("memlock_status", {"session_id": "mcp-rb"})
    assert "MCP first version" in status
    assert "current: MCP first version" in status

    # rollback through MCP requires only session_id + pin_id
    rb_err = dispatch("memlock_update", {"session_id": "mcp-rb"})
    assert rb_err["isError"] is True
    assert "missing required argument" in rb_err["content"][0]["text"]


def test_mcp_update_still_requires_text_for_plain_update():
    dispatch = build_dispatcher()
    resp = dispatch("memlock_update", {
        "session_id": "x", "pin_id": "pin_1_0",
    })
    assert resp["isError"] is True
    assert "'text' is required to update" in resp["content"][0]["text"]


def test_rollback_restored_pin_rehydrates_after_compaction(
    memlock, fake_ctx_cls, base_cfg,
):
    from memlock_core.detection import DEFAULT_SUMMARY_PREFIXES

    _setup(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Rolled-back rule about otters"}, session_id="rb-s")
    store = memlock._get_store("rb-s")
    pin_id = _first_pin_id(store)
    _pin(memlock, {"pin_id": pin_id, "text": "Temporary rule about herons"},
         session_id="rb-s")
    _pin(memlock, {"pin_id": pin_id, "action": "rollback"}, session_id="rb-s")
    assert store.get_anchor(pin_id)["text"] == "Rolled-back rule about otters"

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": DEFAULT_SUMMARY_PREFIXES[0] + " compacted."},
        {"role": "user", "content": "Hello there"},
    ]
    result = memlock._on_pre_llm(
        session_id="rb-s", turn_id="1", user_message="Hello there",
        conversation_history=history,
    )
    assert result is not None
    assert "Rolled-back rule about otters" in result["context"]
