"""Tests for MemLock: behaviour with nothing pinned (effectively off)."""
from detection import SUMMARY_PREFIX



def test_no_anchors_means_no_injection(plugin, memlock):
    """With no anchors at all, the hook never injects, even on compaction."""
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": SUMMARY_PREFIX + " Earlier turns compacted."},
        {"role": "user", "content": "Hello"},
    ]
    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is None


def test_missing_session_id_is_noop(plugin, memlock):
    """Hook without a session id does nothing rather than guessing."""
    assert memlock._on_pre_llm(session_id="", conversation_history=[]) is None
