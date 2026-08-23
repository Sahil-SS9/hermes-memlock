"""Stage 1 tests: harness-agnostic core extraction.

Guards the two properties the v0.5.0 spec makes non-negotiable:

1. Import-path independence — memlock_core must import and run with no
   harness module present at all (no Hermes names, no Claude Code names),
   including from a bare subprocess with only stdlib available.
2. Marker parameterisation — compaction summary prefixes and the reminder
   marker are constructor parameters; a host with its own compact boundary
   wording needs none of the defaults.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# A Claude-Code-shaped compact boundary marker, used to prove the core can
# carry a non-Hermes marker without importing anything.
CC_MARKER = (
    "This session is being continued from a previous conversation that ran "
    "out of context."
)


def _core_service(summary_prefixes=None, reminder_marker=None, **cfg):
    from memlock_core import MemlockService

    kwargs = {}
    if summary_prefixes is not None:
        kwargs["summary_prefixes"] = summary_prefixes
    if reminder_marker is not None:
        kwargs["reminder_marker"] = reminder_marker
    return MemlockService(cfg, **kwargs)


class TestImportPathIndependence:
    """The core must work with zero harness modules installed."""

    def test_core_imports_in_bare_subprocess(self):
        """memlock_core imports in a fresh interpreter with an empty env.

        No HERMES_HOME, no sys.path tricks beyond the repo root, and a
        startup check that fails the process if any harness module resolved.
        """
        code = (
            "import sys\n"
            "banned = [m for m in sys.modules if 'hermes' in m.lower() "
            "or 'claude' in m.lower() or m.startswith('agent')]\n"
            "assert not banned, banned\n"
            "import memlock_core\n"
            "print('ok', memlock_core.__version__)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=60,
            cwd=str(_REPO),
            env={"PATH": "/usr/bin:/bin", "HOME": str(_REPO / ".nonexistent-home")},
        )
        assert proc.returncode == 0, proc.stderr
        assert "ok " in proc.stdout

    def test_no_harness_strings_anywhere_in_core_source(self):
        """Static scan: no harness imports or harness-specific literals.

        Banned are Python import/identifier references to harness modules
        (the real coupling risk) and hard-coded harness home paths.  Bare
        env-var NAMES like HERMES_HOME are legitimate host-neutral config
        contracts and are allowed.
        """
        import re as _re

        banned_patterns = (
            # any import/from statement naming a harness module
            _re.compile(r"^\s*(?:import|from)\s+\S*(hermes|claude|context_compressor)", _re.MULTILINE | _re.IGNORECASE),
            # harness-qualified identifiers used in code (agent.context_compressor etc.)
            _re.compile(r"\b(?:hermes|claude|kenseiagent)[\w.]*\s*\.", _re.IGNORECASE),
            # hard-coded harness home directory literals
            _re.compile(r"~/\.hermes|['\"]/?home/\S*\.hermes"),
        )
        hits = []
        for py_file in (_REPO / "memlock_core").glob("*.py"):
            text = py_file.read_text()
            for pat in banned_patterns:
                if pat.search(text):
                    hits.append(f"{py_file.name}: {pat.pattern}")
        assert hits == [], (
            "harness references must not appear in memlock_core/ source"
        )

    def test_detection_has_no_conditional_harness_import(self):
        """The old try-import of agent.context_compressor is gone."""
        text = (_REPO / "memlock_core" / "detection.py").read_text()
        assert "context_compressor" not in text
        assert "try:" not in text.split("def _message_text")[0]

    def test_core_service_full_lifecycle_without_defaults(
        self, monkeypatch, tmp_path
    ):
        """Pin → compact with a custom marker → rehydrated, all via core."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        svc = _core_service(
            summary_prefixes=[CC_MARKER],
            inject="on-drift",
        )
        sid = "core-s"
        svc.on_start(session_id=sid)
        store = svc.ensure_store(sid)
        out = svc.pin_handler({"text": "Always reply in bullet points",
                               "priority": 80}, session_id=sid)
        assert "Pinned instruction" in out

        history = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": CC_MARKER + " earlier stuff"},
            {"role": "user", "content": "hi"},
        ]
        # Turn 50 forces the safety net so drift alone triggers injection.
        for _ in range(49):
            svc.pre_llm_turn(
                session_id=sid, user_message="hi",
                conversation_history=[{"role": "system", "content": "sys"},
                                      {"role": "user", "content": "hi"}],
            )
        result = svc.pre_llm_turn(
            session_id=sid, user_message="hi", conversation_history=history,
        )
        assert result is not None
        assert "bullet points" in result["context"]
        # The custom marker was the compaction boundary actually detected.
        assert store.last_summary_hash is not None


class TestMarkerParameterisation:
    """summary_prefixes / reminder_marker are constructor config."""

    def test_default_prefixes_are_the_frozen_literals(self):
        from memlock_core.detection import DEFAULT_SUMMARY_PREFIXES

        assert any(p.startswith("[CONTEXT COMPACTION") for p in DEFAULT_SUMMARY_PREFIXES)
        assert "[CONTEXT SUMMARY]:" in DEFAULT_SUMMARY_PREFIXES

    def test_custom_marker_detected_default_not(self):
        from memlock_core.detection import find_summary

        history = [
            {"role": "assistant", "content": CC_MARKER + " body here"},
        ]
        idx, body = find_summary(history)
        assert idx is None  # default prefixes don't match a foreign marker

        idx, body = find_summary(history, prefixes=[CC_MARKER])
        assert idx == 0
        assert body == "body here"

    def test_empty_prefix_list_disables_summary_detection(self):
        from memlock_core.detection import find_summary

        history = [{"role": "assistant", "content": "[CONTEXT SUMMARY]: x"}]
        idx, _ = find_summary(history, prefixes=[])
        assert idx is None

    def test_reminder_marker_parameterisation(self):
        svc = _core_service(reminder_marker="[Pinned rules]")
        block = svc.build_reminder_block([{"reminder": "use bullets"}], [])
        assert block is not None
        assert block.startswith("[Pinned rules]")
        assert "Standing instructions" not in block

    def test_service_defaults_match_plugin_constants(self):
        from memlock_core import REMINDER_MARKER

        svc = _core_service()
        assert svc.reminder_marker == REMINDER_MARKER

    def test_pre_llm_uses_service_prefixes_not_globals(self):
        """Two services with different markers do not interfere."""
        monkey_free_history = [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": CC_MARKER + " old turns"},
            {"role": "user", "content": "go"},
        ]
        mine = _core_service(summary_prefixes=[CC_MARKER])
        theirs = _core_service(summary_prefixes=["###OTHER###"])

        for svc, expect_compaction in ((mine, True), (theirs, False)):
            sid = f"s-{expect_compaction}"
            svc.on_start(session_id=sid)
            svc.ensure_store(sid).add_anchor(
                "p1", "always use tabs", "tabs", 50, ["tabs"], pinned=True,
            )
            # Drive past the safety net so only a real compaction audits.
            for turn_i in range(39):
                svc.pre_llm_turn(session_id=sid, user_message="go",
                                 conversation_history=[
                                     {"role": "system", "content": "s"},
                                     {"role": "user", "content": "go"}],
                                 )
            result = svc.pre_llm_turn(
                session_id=sid, user_message="go",
                conversation_history=monkey_free_history,
            )
            store = svc.get_store(sid)
            recorded = store.last_summary_hash is not None
            assert recorded is expect_compaction
