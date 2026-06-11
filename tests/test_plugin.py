"""Tests for MemLock plugin — core integration tests. Run with pytest."""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_PDIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PDIR))
_INIT = str(_PDIR / "__init__.py")
spec = importlib.util.spec_from_file_location(
    "memlock", _INIT, submodule_search_locations=[str(_PDIR)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules["memlock"] = mod
spec.loader.exec_module(mod)


class FakeCtx:
    def __init__(self, cfg, session_id="test-session"):
        self._cfg = cfg
        self.session_id = session_id
        self.hooks = {}
        self.tools = {}
        self.commands = {}

    @property
    def config(self):
        return self._cfg

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"handler": handler, "schema": schema}

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler


@pytest.fixture
def tmpdir():
    """Isolated temp store."""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def plugin(tmpdir):
    """Register the plugin in an isolated temp environment."""
    os.environ["HERMES_HOME"] = str(Path(tmpdir) / "hermes_home")
    os.makedirs(os.environ["HERMES_HOME"], exist_ok=True)
    cfg = {
        "max_slots": 8, "max_reminder_chars": 600,
        "hard_reinject_turns": 40,
        "alert_floor": 70, "alert_cooldown_s": 1800,
        "drift_threshold": 0.5,
        "detection": "keyword",
        "anchors": [],
    }
    fc = FakeCtx({"memlock": cfg}, session_id="test-s")
    mod._stores.clear()
    mod._session_turns.clear()
    mod._cfg = mod._merge_cfg(cfg)
    mod.register(fc)
    mod._on_start(session_id="test-s")
    return fc, cfg


def test_pin_and_rehydrate(plugin):
    """Core test: pin → compact → detect drift → rehydrate."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                      priority=80, probes=["bullets", "points"], pinned=True)
    assert "pin_1" in store.anchors()

    # Simulate compaction: summary_idx=1 (2nd message is summary)
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": mod.SUMMARY_PREFIX + " Earlier turns compacted."},
        {"role": "user", "content": "Tell me about X"},
    ]

    summary_idx, body, _ = mod.find_summary(history)
    assert summary_idx == 1

    active, _ = mod.split_context(history, summary_idx)
    alive, drifted = mod.audit_anchors(
        store.sorted_anchors(), active, drift_threshold=0.5
    )
    # "bullets" and "points" not in active region
    assert "pin_1" in drifted

    selected, remaining = mod._select_casualties(
        store, drifted, max_slots=8, max_chars=600
    )
    block = mod._build_reminder_block(selected, remaining)
    assert block is not None
    assert mod.REMINDER_MARKER in block
    assert "bullet" in block


def test_safety_net(plugin):
    """Safety net: reinject top anchors even without drift after 40 turns."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_1", "Always use British English Standard",
                     "British English", priority=80,
                     probes=["British English"], pinned=True)
    # Bump turn counter above threshold
    mod._session_turns["test-s"] = 50

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Write some text"},
    ]

    result = mod._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Write some text",
        conversation_history=history,
    )
    assert result is not None
    assert "British" in result.get("context", "")
    assert mod.REMINDER_MARKER in result.get("context", "")


def test_no_compaction_noop(plugin):
    """No compaction, no safety net → no rehydration."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    mod._session_turns["test-s"] = 5  # below 40 threshold

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
    ]

    result = mod._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is None


def test_static_anchors_seeded(plugin):
    """Static anchors from config survive session start."""
    _, cfg = plugin
    cfg["anchors"] = [
        {"id": "test-format", "text": "Use bullet points",
         "priority": 80, "probes": ["bullets", "points"], "pinned": True},
    ]
    mod._cfg = mod._merge_cfg(cfg)
    mod._on_start(session_id="test-s-2")

    store = mod._get_store("test-s-2")
    assert store is not None
    anchors = store.anchors()
    assert "test-format" in anchors
    assert anchors["test-format"]["priority"] == 80


def test_pin_tool_handler(plugin):
    """guard_pin tool creates anchor via _pin_handler."""
    _, cfg = plugin
    result = mod._pin_handler(
        {"text": "Use bullet points always", "priority": 90}
    )
    assert "Pinned instruction" in result

    store = mod._ensure_store("test-s")
    anchors = store.anchors()
    assert any("bullet" in a["text"] for a in anchors.values())


def test_unpin(plugin):
    """Unpin removes a previously pinned anchor."""
    _, cfg = plugin
    result = mod._pin_handler(
        {"text": "Use bullet points first for unpin test", "priority": 80}
    )
    assert "Pinned instruction" in result

    store = mod._ensure_store("test-s")
    anchors = store.anchors()
    anchor_ids = list(anchors.keys())
    assert len(anchor_ids) == 1
    uid = anchor_ids[0]

    result = mod._pin_handler({"unpin": uid})
    assert "Unpinned" in result

    updated = store.anchors()
    assert uid not in updated


def test_status_command(plugin):
    """Status command returns structured output."""
    fc, cfg = plugin
    handler = fc.commands.get("guard")
    assert handler is not None
    result = handler("")
    assert "MemLock" in result or "integrity" in result


def test_rehydration_with_prompt_optimizer_prefix(plugin):
    """Compaction with 'CONTEXT SUMMARY' prefix is still detected."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_2", "Write in British English", "British English",
                     priority=80, probes=["British English"], pinned=True)
    mod._session_turns["test-s"] = 50

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": "[CONTEXT SUMMARY]: Compacted earlier content"},
        {"role": "user", "content": "Hello"},
    ]

    result = mod._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is not None


def test_safety_net_no_false_positive(plugin):
    """No rehydration before hard_reinject_turns threshold."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    mod._session_turns["test-s"] = 30

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
    ]

    result = mod._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is None


def test_drift_threshold_matters(plugin):
    """drift threshold testing: few probe hits trigger drift."""
    _, cfg = plugin
    store = mod._ensure_store("test-s")
    store.add_anchor("pin_3", "Include diagrams", "Include visual diagrams",
                     priority=70, probes=["diagram", "visual", "chart", "figure", "graph"],
                     pinned=True)
    mod._session_turns["test-s"] = 50

    # Active region has "diagram" but not chart/figure/graph
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": mod.SUMMARY_PREFIX[:80] + " Earlier turns compacted."},
        {"role": "user", "content": "Draw a diagram"},
    ]

    result = mod._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Draw a diagram",
        conversation_history=history,
    )
    # 1/5 = 0.2 ≥ 0.5? No. So pin should drift.
    assert result is not None, "Expected rehydration from drift"
