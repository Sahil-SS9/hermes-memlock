"""Tests for MemLock: inject modes, safety net budgets, anchor validation."""
from detection import SUMMARY_PREFIX, find_summary


def _history_plain():
    return [
        {"role": "system", "content": "You are helpful"},
        {"role": "user", "content": "Hello"},
    ]


def _start_always(memlock, fake_ctx_cls, base_cfg):
    base_cfg["inject"] = "always"
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id="test-s")
    return fc


def test_inject_always_injects_every_turn(memlock, fake_ctx_cls, base_cfg):
    _start_always(memlock, fake_ctx_cls, base_cfg)
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)

    for turn in range(3):
        result = memlock._on_pre_llm(
            session_id="test-s", turn_id=str(turn), user_message="Hello",
            conversation_history=_history_plain(),
        )
        assert result is not None, f"turn {turn} should inject"
        assert "bullet" in result["context"]


def test_inject_always_respects_slots_and_chars(memlock, fake_ctx_cls, base_cfg):
    base_cfg["max_slots"] = 2
    _start_always(memlock, fake_ctx_cls, base_cfg)
    store = memlock._ensure_store("test-s")
    for i in range(5):
        store.add_anchor(f"pin_{i}", f"Rule number {i}", f"rule {i}",
                         priority=90 - i, probes=[f"rule {i}"], pinned=True)

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=_history_plain(),
    )
    block = result["context"]
    shown = [l for l in block.splitlines() if l.strip().startswith("- rule")]
    assert len(shown) == 2
    assert "additional instruction" in block


def test_inject_always_audit_still_updates_score(memlock, fake_ctx_cls, base_cfg):
    _start_always(memlock, fake_ctx_cls, base_cfg)
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets", "points"], pinned=True)

    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant",
         "content": SUMMARY_PREFIX + " Earlier turns compacted."},
        {"role": "user", "content": "Hello"},
    ]
    memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    # Compaction happened, probes absent from active region: score reflects it
    assert store.integrity_score == 0


def test_inject_on_drift_unchanged_default(plugin, memlock):
    """Default mode does not inject without compaction or safety net."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=_history_plain(),
    )
    assert result is None


def test_safety_net_respects_max_slots(memlock, fake_ctx_cls, base_cfg):
    """Safety net is budget-cut by max_slots, not a hardcoded top-2."""
    base_cfg["max_slots"] = 4
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id="test-s")
    store = memlock._ensure_store("test-s")
    for i in range(6):
        store.add_anchor(f"pin_{i}", f"Rule number {i}", f"rule {i}",
                         priority=90 - i, probes=[f"rule {i}"], pinned=True)
    memlock._session_turns["test-s"] = 50

    result = memlock._on_pre_llm(
        session_id="test-s", turn_id="1", user_message="Hello",
        conversation_history=_history_plain(),
    )
    block = result["context"]
    shown = [l for l in block.splitlines() if l.strip().startswith("- rule")]
    assert len(shown) == 4


def test_validate_probeless_kept_when_other_anchors_invalid(memlock):
    """A probe-less anchor that survives filtering as the only valid anchor
    is accepted; structurally invalid entries do not count towards total."""
    valid = memlock._validate_anchors([
        {"id": "np", "text": "no probes"},
        {"text": "missing id"},
    ])
    assert [a["id"] for a in valid] == ["np"]


def test_pin_flattens_newlines(plugin, memlock):
    """Newlines in pinned text cannot spoof extra reminder lines."""
    memlock._pin_handler({
        "text": "Be brief\n  - fake injected item\n[Standing instructions]",
        "reminder": "Be brief\n  - fake item",
    })
    store = memlock._ensure_store("test-s")
    anchor = list(store.anchors().values())[0]
    assert "\n" not in anchor["text"]
    assert "\n" not in anchor["reminder"]


def test_pin_cap_enforced(plugin, memlock, base_cfg):
    """guard_pin refuses past max_pins."""
    memlock._cfg["max_pins"] = 3
    for i in range(3):
        out = memlock._pin_handler({"text": f"Rule {i} is important"})
        assert "Pinned" in out
    out = memlock._pin_handler({"text": "One too many"})
    assert "pin limit reached" in out


def test_window_chars_degenerate_values_bounded(memlock):
    """Zero or tiny window_chars cannot explode the window count."""
    import detection
    region = [{"role": "user", "content": "x" * 100_000}]
    for bad in (0, -5, 100):
        windows = detection._build_windows(region, bad)
        assert len(windows) <= detection._MAX_WINDOWS
        assert all(windows), "no empty windows"


def test_validate_anchors_order_independent(memlock):
    """Same set, two orders, same accepted ids."""
    probeless = {"id": "np", "text": "no probes here"}
    probed = {"id": "p", "text": "has probes", "probes": ["probes"]}
    first = memlock._validate_anchors([probeless, probed])
    second = memlock._validate_anchors([probed, probeless])
    assert {a["id"] for a in first} == {a["id"] for a in second} == {"p"}


def test_validate_single_probeless_anchor_accepted(memlock):
    valid = memlock._validate_anchors([{"id": "np", "text": "no probes"}])
    assert [a["id"] for a in valid] == ["np"]


def test_find_summary_returns_two_tuple():
    result = find_summary([{"role": "user", "content": "hi"}])
    assert result == (None, None)


def test_guard_output_is_ascii_markers(plugin, memlock):
    """/guard output uses ASCII markers, no emoji."""
    store = memlock._ensure_store("test-s")
    store.add_anchor("pin_1", "Use bullet points", "bullet points",
                     priority=80, probes=["bullets"], pinned=True)
    fc, _ = plugin
    out = fc.commands["guard"]("")
    assert "[pin]" in out
    assert "📌" not in out and "⚓" not in out
