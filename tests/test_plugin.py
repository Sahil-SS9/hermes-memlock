"""Tests for MemLock plugin: core integration tests. Run with pytest."""
from detection import SUMMARY_PREFIX



def test_pin_and_rehydrate(plugin, memlock):
    """Core test: pin, compact, detect drift, rehydrate."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets", "points"], pinned=True)
    assert "pin_1" in store.anchors()

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": SUMMARY_PREFIX + " Earlier turns compacted."},
        {"role": "user", "content": "Tell me about X"},
    ]

    summary_idx, body = memlock.find_summary(history)
    assert summary_idx == 1

    active, _ = memlock.split_context(history, summary_idx)
    alive, drifted = memlock.audit_anchors(
        store.sorted_anchors(), active, drift_threshold=0.5
    )
    # "bullets" and "points" not in active region
    assert "pin_1" in drifted

    selected, remaining = memlock._select_casualties(
        store, drifted, max_slots=8, max_chars=600
    )
    block = memlock._build_reminder_block(selected, remaining)
    assert block is not None
    assert memlock.REMINDER_MARKER in block
    assert "bullet" in block


def test_safety_net(plugin, memlock):
    """Safety net: reinject top anchors even without drift after 40 turns."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Always use British English Standard",
                     "British English", priority=80,
                     probes=["British English"], pinned=True)
    memlock._session_turns["test-s"] = 50

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Write some text"},
    ]

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Write some text",
        conversation_history=history,
    )
    assert result is not None
    assert "British" in result.get("context", "")
    assert memlock.REMINDER_MARKER in result.get("context", "")


def test_no_compaction_noop(plugin, memlock):
    """No compaction, no safety net: no rehydration."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    memlock._session_turns["test-s"] = 5

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
    ]

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is None


def test_static_anchors_seeded(plugin, memlock, base_cfg):
    """Static anchors from config survive session start."""
    base_cfg["anchors"] = [
        {"id": "test-format", "text": "Use bullet points",
         "priority": 80, "probes": ["bullets", "points"], "pinned": True},
    ]
    memlock._cfg = memlock._merge_cfg(base_cfg)
    memlock._on_start(session_id="test-s-2")

    store = memlock._get_store("test-s-2")
    assert store is not None
    anchors = store.anchors()
    assert "test-format" in anchors
    assert anchors["test-format"]["priority"] == 80


def test_pin_tool_handler(plugin, memlock):
    """guard_pin tool creates anchor via _pin_handler."""
    result = memlock._pin_handler(
        {"text": "Use bullet points always", "priority": 90}
    )
    assert "Pinned instruction" in result

    store = memlock._ensure_store("test-s")
    anchors = store.anchors()
    assert any("bullet" in a["text"] for a in anchors.values())


def test_unpin(plugin, memlock):
    """Unpin removes a previously pinned anchor."""
    result = memlock._pin_handler(
        {"text": "Use bullet points first for unpin test", "priority": 80}
    )
    assert "Pinned instruction" in result

    store = memlock._ensure_store("test-s")
    anchor_ids = list(store.anchors().keys())
    assert len(anchor_ids) == 1
    uid = anchor_ids[0]

    result = memlock._pin_handler({"unpin": uid})
    assert "Unpinned" in result
    assert uid not in store.anchors()


def test_status_command(plugin, memlock):
    """Status command returns structured output."""
    fc, _ = plugin
    handler = fc.commands.get("guard")
    assert handler is not None
    result = handler("")
    assert "MemLock" in result or "integrity" in result


def test_rehydration_with_prompt_optimizer_prefix(plugin, memlock):
    """Compaction with legacy 'CONTEXT SUMMARY' prefix is still detected."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_2", "Write in British English", "British English",
                     priority=80, probes=["British English"], pinned=True)
    memlock._session_turns["test-s"] = 50

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": "[CONTEXT SUMMARY]: Compacted earlier content"},
        {"role": "user", "content": "Hello"},
    ]

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is not None


def test_safety_net_no_false_positive(plugin, memlock):
    """No rehydration before hard_reinject_turns threshold."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    memlock._session_turns["test-s"] = 30

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
    ]

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is None


def test_drift_threshold_matters(plugin, memlock):
    """Few probe hits in the active region trigger drift."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_3", "Include diagrams", "Include visual diagrams",
                     priority=70,
                     probes=["diagram", "visual", "chart", "figure", "graph"],
                     pinned=True)
    memlock._session_turns["test-s"] = 50

    # Active region has "diagram" only: 1/5 probes < 0.5 threshold
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": SUMMARY_PREFIX + " Earlier turns compacted."},
        {"role": "user", "content": "Draw a diagram"},
    ]

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Draw a diagram",
        conversation_history=history,
    )
    assert result is not None, "Expected rehydration from drift"
