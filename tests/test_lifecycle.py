"""Tests for MemLock — on_session_end save + turn lifecycle."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PDIR))

import store as store_mod
from detection import find_summary, hash_summary_body, split_context


def test_store_create_and_persist():
    """SessionStore creates, saves, and re-loads data."""
    d = tempfile.mkdtemp()
    try:
        os.environ["HERMES_HOME"] = str(Path(d) / "hermes_home")
        os.makedirs(os.environ["HERMES_HOME"], exist_ok=True)

        s1 = store_mod.SessionStore("test-s")
        s1.add_anchor("pin_1", "Use bullet points", "bullet points",
                      priority=80, probes=["bullets"], pinned=True)
        assert "pin_1" in s1.anchors()

        # Create a new instance (simulates reload)
        s2 = store_mod.SessionStore("test-s")
        assert "pin_1" in s2.anchors()
        assert s2.anchors()["pin_1"]["priority"] == 80
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_integrity_score():
    """Integrity score calculation."""
    d = tempfile.mkdtemp()
    try:
        os.environ["HERMES_HOME"] = str(Path(d) / "hermes_home")
        os.makedirs(os.environ["HERMES_HOME"], exist_ok=True)

        s = store_mod.SessionStore("score-test")
        s.add_anchor("a1", "text a1", "t1", priority=80, probes=["a1"], pinned=True)
        s.add_anchor("a2", "text a2", "t2", priority=50, probes=["a2"], pinned=True)

        s.mark_anchor_alive("a1", 1)
        s.mark_anchor_drifted("a2")

        score = s.compute_integrity_score()
        assert score == 50  # 1/2 alive
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_find_summary_detection():
    """SUMMARY_PREFIX detection."""
    from detection import SUMMARY_PREFIX
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " early turns"},
        {"role": "user", "content": "hello"},
    ]
    idx, body, _ = find_summary(history)
    assert idx == 1
    assert "early turns" in body


def test_split_context_excludes_summary():
    """Active region excludes summary content."""
    from detection import SUMMARY_PREFIX
    history = [
        {"role": "system", "content": "System prompt"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " old stuff"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    idx, _, _ = find_summary(history)
    active, summary = split_context(history, idx)
    assert len(active) == 3  # system + 2 after summary
    assert len(summary) == 1  # only the summary message
    # System is always in active
    assert active[0] == history[0]
    assert active[1]["content"] == "hello"
    assert active[2]["content"] == "world"
