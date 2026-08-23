"""Tests for MemLock: import styles and filesystem isolation."""
import importlib.util
import os
import sys
from pathlib import Path

import store as store_mod

_PDIR = Path(__file__).resolve().parent.parent


def test_store_dir_resolved_at_call_time(monkeypatch, tmp_path):
    """HERMES_HOME changed after import is honoured."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert store_mod._store_dir() == first / "memlock"
    monkeypatch.setenv("HERMES_HOME", str(second))
    assert store_mod._store_dir() == second / "memlock"


def test_store_writes_under_tmp_only(isolated_hermes_home, tmp_path):
    """A saved store lands under the isolated home, nowhere else."""
    s = store_mod.SessionStore("iso-check")
    s.add_anchor("a1", "text", "t", priority=50, probes=["text"], pinned=True)
    expected = isolated_hermes_home / "memlock" / "iso-check.json"
    assert expected.exists()
    real = Path(os.path.expanduser("~/.hermes/memlock/iso-check.json"))
    assert not real.exists()


def test_import_works_as_plain_module():
    """__init__.py loads without a package context (absolute-import fallback)."""
    spec = importlib.util.spec_from_file_location(
        "memlock_plain_check", _PDIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, "register")


def test_import_works_as_package():
    """__init__.py loads as a package (relative imports)."""
    name = "memlock_pkg_check"
    spec = importlib.util.spec_from_file_location(
        name, _PDIR / "__init__.py", submodule_search_locations=[str(_PDIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        assert hasattr(mod, "register")
    finally:
        sys.modules.pop(name, None)


# ── two-session isolation (the core promise) ─────────────────────────────
#
# These tests drive the PUBLIC API only (_on_start / _pin_handler /
# _on_pre_llm) with two distinct session_ids and the isolated HERMES_HOME
# fixture, locking in: no cross-session pin leakage, separate disk files,
# compaction events that cannot touch the other session, and durable global
# pins that seed both sessions while surviving a session-local unpin.


def _two_sessions(memlock, fake_ctx_cls, base_cfg):
    """Register once; start sessions alpha and beta through the public API."""
    fc = fake_ctx_cls({"memlock": base_cfg})
    memlock.register(fc)
    memlock._on_start(session_id="sess-alpha")
    memlock._on_start(session_id="sess-beta")
    return fc


def _pin(memlock, kwargs, session_id=None):
    """Pin with the handler; session_id kwarg emulates the dispatch-layer
    forwarding (vanilla Hermes falls back to the last-seen session)."""
    return memlock._pin_handler(dict(kwargs), session_id=session_id)


def test_two_sessions_pins_are_invisible_to_each_other(
    memlock, fake_ctx_cls, base_cfg,
):
    """A pin in one session does not appear in the other's store or on disk."""
    _two_sessions(memlock, fake_ctx_cls, base_cfg)

    out = _pin(memlock, {
        "text": "Alpha-only rule: always show your working",
        "priority": 80,
    }, session_id="sess-alpha")
    assert "Pinned instruction" in out

    alpha = memlock._get_store("sess-alpha")
    beta = memlock._get_store("sess-beta")
    assert any("Alpha-only" in a["text"] for a in alpha.anchors().values())
    assert beta.anchors() == {}

    # Disk files are separate and contain disjoint state. Beta was never
    # written to (no pins, no compaction), so its store file may not exist
    # yet — which is itself isolation evidence.
    import json
    alpha_path = alpha._path
    beta_path = beta._path
    assert alpha_path != beta_path
    alpha_disk = json.loads(alpha_path.read_text())
    assert any("Alpha-only" in a["text"] for a in alpha_disk["anchors"].values())
    if beta_path.exists():
        assert json.loads(beta_path.read_text())["anchors"] == {}
    # Force beta's file into existence and re-check disjointness.
    beta.save()
    assert beta_path.exists()
    assert json.loads(beta_path.read_text())["anchors"] == {}


def test_compaction_in_one_session_does_not_touch_the_other(
    memlock, fake_ctx_cls, base_cfg,
):
    """Drift/rehydration in beta leaves alpha's anchor state untouched."""
    from detection import SUMMARY_PREFIX

    _two_sessions(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Alpha rule about bullet points", "priority": 80},
         session_id="sess-alpha")
    _pin(memlock, {"text": "Beta rule about British English spelling",
                   "priority": 70},
         session_id="sess-beta")
    alpha = memlock._get_store("sess-alpha")
    beta = memlock._get_store("sess-beta")

    # Compaction event in beta: probes absent -> drift + rehydrate.
    history = [
        {"role": "system", "content": "You are helpful"},
        {"role": "assistant", "content": SUMMARY_PREFIX + " compacted."},
        {"role": "user", "content": "Hello"},
    ]
    result = memlock._on_pre_llm(
        session_id="sess-beta", turn_id="1", user_message="Hello",
        conversation_history=history,
    )
    assert result is not None, "beta compaction should rehydrate"
    assert "British English" in result["context"]

    # Alpha: no drift recorded, no compaction timestamp, score untouched.
    assert all(not a["drifted"] for a in alpha.anchors().values())
    assert alpha._data.get("last_summary_hash") is None
    assert alpha._data.get("last_compaction_at") is None
    assert alpha.integrity_score is None
    assert alpha._data["drift_log"] == []
    # Beta recorded the compaction.
    assert beta.last_summary_hash is not None


def test_global_pin_seeds_both_sessions_and_survives_local_unpin(
    memlock, fake_ctx_cls, base_cfg, isolated_hermes_home,
):
    """scope=global seeds BOTH sessions; unpinning in one keeps the durable
    copy intact until it is unpinned again."""
    _two_sessions(memlock, fake_ctx_cls, base_cfg)

    out = _pin(memlock, {
        "text": "Global rule: always use British English",
        "priority": 90,
        "scope": "global",
    }, session_id="sess-alpha")
    assert "Pinned instruction" in out
    gid = next(iter(memlock._get_store("sess-alpha").anchors()))

    # Alpha sees it immediately. Beta was already running when the pin was
    # created: global pins seed at session START (documented lifecycle), so
    # beta picks it up on its next start — restart beta to simulate that.
    assert "British English" in memlock._get_store("sess-alpha").anchors()[gid]["text"]
    memlock._on_start(session_id="sess-beta")
    anchors = memlock._get_store("sess-beta").anchors()
    assert gid in anchors and "British English" in anchors[gid]["text"]

    # Durable copy exists exactly once under persist/.
    persist_dir = isolated_hermes_home / "memlock" / "persist"
    files = list(persist_dir.glob("*.json"))
    assert len(files) == 1

    # Unpin in alpha only.
    out = _pin(memlock, {"unpin": gid}, session_id="sess-alpha")
    assert "Unpinned" in out
    assert gid not in memlock._get_store("sess-alpha").anchors()
    assert gid in memlock._get_store("sess-beta").anchors(), \
        "beta must keep its seeded copy"
    assert len(list(persist_dir.glob("*.json"))) == 1, \
        "durable copy must survive a session-scoped unpin"

    # A NEW session still gets the global pin re-seeded.
    memlock._on_start(session_id="sess-gamma")
    gamma = memlock._get_store("sess-gamma")
    assert gid in gamma.anchors()

    # Unpin globally (from any session): scope=global removes the durable
    # copy too...
    out = _pin(memlock, {"unpin": gid, "scope": "global"}, session_id="sess-beta")
    assert "Unpinned globally" in out
    assert len(list(persist_dir.glob("*.json"))) == 0
    assert gid not in memlock._get_store("sess-beta").anchors()

    # ...and a fresh session no longer sees it.
    memlock._on_start(session_id="sess-delta")
    assert gid not in memlock._get_store("sess-delta").anchors()


def test_sessions_do_not_share_turn_counters_or_reinject_state(
    memlock, fake_ctx_cls, base_cfg,
):
    """Turn accounting is per-session: driving beta to its safety net does
    not push alpha over hard_reinject_turns."""
    from detection import SUMMARY_PREFIX

    _two_sessions(memlock, fake_ctx_cls, base_cfg)
    _pin(memlock, {"text": "Alpha rule with distinctive zephyr probes",
                   "priority": 80},
         session_id="sess-alpha")

    # Drive beta far past the safety-net threshold without touching alpha.
    for turn in range(45):
        memlock._on_pre_llm(
            session_id="sess-beta", turn_id=str(turn), user_message=f"msg {turn}",
            conversation_history=[
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": SUMMARY_PREFIX + f" s{turn}"},
                {"role": "user", "content": f"msg {turn}"},
            ],
        )

    alpha = memlock._get_store("sess-alpha")
    # Alpha never ran pre_llm: no turns counted, nothing injected.
    assert memlock._session_turns.get("sess-alpha", 0) == 0
    assert alpha._data.get("last_reinject_turn", 0) == 0

    # Alpha at turn 5 (well below threshold 40) must NOT inject.
    result = memlock._on_pre_llm(
        session_id="sess-alpha", turn_id="5", user_message="hello",
        conversation_history=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
        ],
    )
    assert result is None
