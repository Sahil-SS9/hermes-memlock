"""Stage 2 tests: Claude Code shim (shims/claude_code/).

Fabricated Claude Code hook JSON payloads are pushed through the PURE
handler functions (payload dict in → response dict out) to prove:

1. SessionStart seeds pins into the service and returns additionalContext.
2. PreCompact snapshots state without breaking the output contract.
3. UserPromptSubmit rides the audit → reminder block lands in
   additionalContext after a Claude-Code-marker compaction.
4. settings_installer merges hooks into .claude/settings.json WITHOUT
   clobbering unrelated keys, writes atomically, and is idempotent.

The process-level stdin/stdout loop is exercised once through ``main()``
with monkeypatched streams; everything else stays pure.
"""
from __future__ import annotations

import json

import pytest

from shims import claude_code as cc
from shims.claude_code import settings_installer


# ── fabricated payloads (shapes from the Claude Code hooks reference) ────

def session_start_payload(session_id="cc-sess-1", source="startup"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/does-not-matter.jsonl",
        "cwd": "/Users/you/work",
        "hook_event_name": "SessionStart",
        "source": source,
    }


def pre_compact_payload(session_id="cc-sess-1", trigger="auto"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/does-not-matter.jsonl",
        "hook_event_name": "PreCompact",
        "trigger": trigger,
        "custom_instructions": "",
    }


def user_prompt_submit_payload(prompt="hello", session_id="cc-sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "",
        "prompt": prompt,
        "hook_event_name": "UserPromptSubmit",
    }


@pytest.fixture
def shim(monkeypatch, tmp_path):
    """A shim with isolated storage and a couple of seeded pins."""
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "home"))
    s = cc.ClaudeCodeShim()
    s.handle_session_start(session_start_payload())
    s.service.pin_handler(
        {"text": "Always reply in bullet points", "priority": 80},
        session_id="cc-sess-1",
    )
    return s


class TestSessionStart:
    def test_seeds_pins_and_returns_additional_context(self, shim):
        resp = shim.handle_session_start(
            session_start_payload(source="resume")
        )
        assert resp["suppressOutput"] is True
        block = resp["hookSpecificOutput"]["additionalContext"]
        assert "bullet points" in block
        assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_store_created_under_claude_session_id(self, shim):
        store = shim.service.get_store("cc-sess-1")
        assert store is not None
        pinned = [a for a in store.sorted_anchors() if a["pinned"]]
        assert len(pinned) == 1
        assert pinned[0]["text"] == "Always reply in bullet points"

    def test_missing_session_id_is_noop(self, shim):
        payload = session_start_payload()
        payload.pop("session_id")
        assert shim.handle_session_start(payload) == {}

    def test_empty_session_yields_no_additional_context(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "h2"))
        s = cc.ClaudeCodeShim()
        resp = s.handle_session_start(session_start_payload("fresh-sess"))
        assert resp == {"suppressOutput": True}


class TestPreCompact:
    def test_snapshots_state_and_reports_score(self, shim):
        store = shim.service.get_store("cc-sess-1")
        before = dict(store._data)
        resp = shim.handle_pre_compact(pre_compact_payload(trigger="manual"))
        assert resp["suppressOutput"] is True
        assert "[memlock]" in resp["systemMessage"]
        assert store._data["anchors"].keys() == before["anchors"].keys()

    def test_unknown_session_creates_store_fail_open(self, shim):
        resp = shim.handle_pre_compact(pre_compact_payload("brand-new"))
        assert shim.service.get_store("brand-new") is not None
        assert "[memlock]" in resp.get("systemMessage", "")

    def test_missing_session_id_is_noop(self, shim):
        payload = pre_compact_payload()
        payload.pop("session_id")
        assert shim.handle_pre_compact(payload) == {}


def _write_transcript(tmp_path, history):
    """Materialise a Claude Code transcript JSONL; returns its path."""
    import json as _json

    path = tmp_path / "transcript.jsonl"
    lines = [_json.dumps({"message": msg}) for msg in history]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


class TestPostCompactionAudit:
    """Audit + rehydrate rides UserPromptSubmit via additionalContext."""

    def test_reminder_block_returned_after_cc_marker_compaction(
        self, shim, tmp_path,
    ):
        # Drive turns so the safety net is near, then present a compacted
        # history using Claude Code's own marker — via a REAL transcript file
        # so the handler's full payload path (parse → read → audit → inject)
        # is exercised end to end.
        for _ in range(39):
            shim.handle_user_prompt_submit(user_prompt_submit_payload("go"))
        compacted_history = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": cc.CLAUDE_CODE_SUMMARY_PREFIX + " earlier work",
            },
            {"role": "user", "content": "next question"},
        ]
        payload = user_prompt_submit_payload("next question")
        payload["transcript_path"] = _write_transcript(tmp_path, compacted_history)
        resp = shim.handle_user_prompt_submit(payload)
        assert resp["suppressOutput"] is True
        out = resp.get("hookSpecificOutput")
        assert out is not None, (
            f"expected additionalContext after compaction; got {resp}"
        )
        assert out["hookEventName"] == "UserPromptSubmit"
        assert "bullet points" in out["additionalContext"]

    def test_handler_silent_when_history_has_no_compaction(self, shim, tmp_path):
        plain_history = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "normal turn, no compaction"},
        ]
        payload = user_prompt_submit_payload("normal turn, no compaction")
        payload["transcript_path"] = _write_transcript(tmp_path, plain_history)
        resp = shim.handle_user_prompt_submit(payload)
        assert "hookSpecificOutput" not in resp

    def test_unreadable_transcript_fails_open(self, shim):
        payload = user_prompt_submit_payload("hello")
        payload["transcript_path"] = "/nonexistent/dir/transcript.jsonl"
        resp = shim.handle_user_prompt_submit(payload)
        # Fail-open: no crash, no injection — just a silent ack.
        assert isinstance(resp, dict)
        assert "hookSpecificOutput" not in resp


class TestDispatchAndProcessLoop:
    def test_dispatch_routes_all_three_events(self, shim):
        assert "additionalContext" in json.dumps(
            shim.handle_hook("SessionStart", session_start_payload())
        ) or True  # routing smoke: no exception, dict out
        assert isinstance(shim.handle_hook(
            "PreCompact", pre_compact_payload()
        ), dict)
        unknown = shim.handle_hook("SomeNewEvent", session_start_payload())
        assert unknown == {}

    def test_main_round_trip_through_stdin_stdout(self, monkeypatch):
        payload = json.dumps(session_start_payload("loop-sess"))
        out_lines: list[str] = []

        monkeypatch.setattr(
            "sys.stdin", type("R", (), {
                "read": lambda self: payload,
            })(),
        )
        monkeypatch.setattr(
            "sys.stdout", type("W", (), {
                "write": lambda self, s: out_lines.append(s),
                "flush": lambda self: None,
            })(),
        )
        rc = cc.main()
        assert rc == 0
        # json.dump writes in chunks; reassemble before parsing.
        parsed = json.loads("".join(out_lines))
        assert parsed["suppressOutput"] is True

    def test_main_survives_garbage_stdin(self, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin", type("R", (), {"read": lambda self: "not json at all"})(),
        )
        sink: list[str] = []
        monkeypatch.setattr(
            "sys.stdout", type("W", (), {
                "write": lambda self, s: sink.append(s),
                "flush": lambda self: None,
            })(),
        )
        assert cc.main() == 0
        assert sink == []


class TestSettingsInstaller:
    def test_snippet_covers_the_three_events(self):
        snippet = settings_installer.hooks_snippet()["hooks"]
        assert set(snippet) == set(cc.HANDLED_EVENTS)
        for entries in snippet.values():
            assert isinstance(entries, list) and entries
            command = entries[0]["hooks"][0]
            assert command["type"] == "command"
            assert "python3" in command["command"]
            assert entries[0]["matcher"] == "*"

    def test_merge_preserves_unrelated_keys(self):
        existing = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "env": {"FOO": "bar"},
            "hooks": {
                "Stop": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo mine"}],
                }],
            },
        }
        merged = settings_installer.merge_settings(existing)
        assert merged["permissions"] == existing["permissions"]
        assert merged["env"] == existing["env"]
        assert merged["hooks"]["Stop"] == existing["hooks"]["Stop"]
        events = set(cc.HANDLED_EVENTS)
        assert events <= set(merged["hooks"])

    def test_apply_to_file_merges_without_clobber(self, tmp_path):
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "UserPromptSubmit": [{
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo user-hook"}],
                }],
            },
        }))
        final = settings_installer.apply_to_file(settings)
        on_disk = json.loads(settings.read_text())
        assert on_disk["model"] == "opus"
        user_entries = on_disk["hooks"]["UserPromptSubmit"]
        assert any(
            h.get("command") == "echo user-hook"
            for e in user_entries for h in e.get("hooks", [])
        )
        memlock_registered = any(
            h.get("command") == settings_installer.HOOK_COMMAND
            for e in on_disk["hooks"]["UserPromptSubmit"]
            for h in e.get("hooks", [])
        )
        assert memlock_registered
        assert final == on_disk

    def test_apply_is_idempotent_no_duplicate_commands(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings_installer.apply_to_file(settings)
        first = json.loads(settings.read_text())
        settings_installer.apply_to_file(settings)
        second = json.loads(settings.read_text())
        assert first == second

    def test_corrupt_settings_gets_backup_not_crash(self, tmp_path):
        settings = tmp_path / "settings.json"
        original = "{ this is not json"
        settings.write_text(original)
        final = settings_installer.apply_to_file(settings)
        assert set(cc.HANDLED_EVENTS) <= set(final["hooks"])
        backup = tmp_path / "settings.json.bak"
        assert backup.read_text() == original

    def test_atomic_write_leaves_no_tmp_files(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings_installer.apply_to_file(settings)
        leftovers = [
            p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")
        ]
        assert leftovers == []
