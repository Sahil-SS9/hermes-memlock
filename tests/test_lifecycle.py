"""Tests for MemLock: store persistence and detection lifecycle."""
import store as store_mod
from detection import SUMMARY_PREFIX, find_summary, split_context


def test_store_create_and_persist():
    """SessionStore creates, saves, and re-loads data."""
    s1 = store_mod.SessionStore("test-s")
    s1.add_anchor("pin_1", "Use bullet points", "bullet points",
                  priority=80, probes=["bullets"], pinned=True)
    assert "pin_1" in s1.anchors()

    # New instance simulates reload
    s2 = store_mod.SessionStore("test-s")
    assert "pin_1" in s2.anchors()
    assert s2.anchors()["pin_1"]["priority"] == 80


def test_integrity_score():
    """Integrity score calculation."""
    s = store_mod.SessionStore("score-test")
    s.add_anchor("a1", "text a1", "t1", priority=80, probes=["a1"], pinned=True)
    s.add_anchor("a2", "text a2", "t2", priority=50, probes=["a2"], pinned=True)

    s.mark_anchor_alive("a1", 1)
    s.mark_anchor_drifted("a2")

    assert s.compute_integrity_score() == 50


def test_find_summary_detection():
    """SUMMARY_PREFIX detection."""
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " early turns"},
        {"role": "user", "content": "hello"},
    ]
    idx, body = find_summary(history)
    assert idx == 1
    assert "early turns" in body


def test_split_context_excludes_summary():
    """Active region excludes summary content."""
    history = [
        {"role": "system", "content": "System prompt"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " old stuff"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    idx, _ = find_summary(history)
    active, summary = split_context(history, idx)
    assert len(active) == 3  # system + 2 after summary
    assert len(summary) == 1
    assert active[0] == history[0]
    assert active[1]["content"] == "hello"
    assert active[2]["content"] == "world"


def test_on_session_end_saves(plugin, memlock, isolated_hermes_home):
    """on_session_end persists the store to disk."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    memlock._on_end(session_id="test-s")
    assert (isolated_hermes_home / "memlock" / "test-s.json").exists()
