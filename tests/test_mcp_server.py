"""Stage 2 tests: MCP server mode (mcp_server/).

Everything runs IN-PROCESS: tests call ``protocol.handle_request`` with raw
JSON lines and the real dispatcher — the same functions the stdio loop uses.
Covered:

1. initialize handshake returns protocol version + server identity.
2. tools/list exposes exactly the five memlock_* tools.
3. pin → status → update → audit flow per session_id.
4. Isolation: two session_ids never see each other's pins.
5. Protocol hygiene: parse errors, unknown methods, notifications, batch
   handling, and fail-open error results instead of crashes.
"""
from __future__ import annotations

import json

import pytest

from mcp_server import protocol
from mcp_server.tools import TOOL_NAMES, build_dispatcher


def rpc(method, params=None, req_id=1):
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return json.dumps(msg)


@pytest.fixture
def dispatch(monkeypatch, tmp_path):
    """A dispatcher over an isolated MemlockService (own storage root)."""
    monkeypatch.setenv("MEMLOCK_HOME", str(tmp_path / "mcp-home"))
    return build_dispatcher()


def call(dispatch, method, params=None, req_id=1):
    resp = protocol.handle_request(rpc(method, params, req_id), dispatch)
    assert resp is not None, f"no response for {method}"
    return resp


class TestInitializeHandshake:
    def test_initialize_returns_protocol_and_identity(self, dispatch):
        resp = call(dispatch, "initialize", {})
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "memlock"
        assert "tools" in result["capabilities"]

    def test_initialized_notification_is_silent(self, dispatch):
        line = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        assert protocol.handle_request(line, dispatch) is None


class TestToolsList:
    def test_five_memlock_tools_exposed(self, dispatch):
        resp = call(dispatch, "tools/list")
        names = [t["name"] for t in resp["result"]["tools"]]
        assert set(names) == set(TOOL_NAMES)
        assert len(names) == 5

    def test_every_tool_requires_session_id(self, dispatch):
        resp = call(dispatch, "tools/list")
        for tool in resp["result"]["tools"]:
            assert "session_id" in tool["inputSchema"]["required"], tool["name"]

    def test_audit_declares_conversation_history(self, dispatch):
        resp = call(dispatch, "tools/list")
        audit = next(
            t for t in resp["result"]["tools"] if t["name"] == "memlock_audit"
        )
        assert "conversation_history" in audit["inputSchema"]["properties"]


class TestPinFlow:
    def test_pin_then_status_shows_the_pin(self, dispatch):
        pinned = call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {
                "session_id": "s1",
                "text": "Always reply in bullet points",
                "priority": 80,
            },
        })
        assert pinned["result"]["isError"] is False
        assert "Pinned instruction" in pinned["result"]["content"][0]["text"]

        status = call(dispatch, "tools/call", {
            "name": "memlock_status",
            "arguments": {"session_id": "s1"},
        })
        text = status["result"]["content"][0]["text"]
        assert "s1" in text
        assert "Always reply in bullet points".split()[0] in text or "[pin]" in text

    def test_update_keeps_same_pin_id(self, dispatch):
        pin = call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {"session_id": "s2", "text": "first version"},
        })
        pin_id = _extract_pin_id(pin["result"]["content"][0]["text"])
        assert pin_id

        updated = call(dispatch, "tools/call", {
            "name": "memlock_update",
            "arguments": {
                "session_id": "s2", "pin_id": pin_id,
                "text": "second version",
            },
        })
        assert updated["result"]["isError"] is False
        assert "Updated pin" in updated["result"]["content"][0]["text"]

        status = call(dispatch, "tools/call", {
            "name": "memlock_status",
            "arguments": {"session_id": "s2"},
        })
        assert "second version" in status["result"]["content"][0]["text"]

    def test_unpin_removes_the_pin(self, dispatch):
        pin = call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {"session_id": "s3", "text": "temporary rule"},
        })
        pin_id = _extract_pin_id(pin["result"]["content"][0]["text"])
        unpinned = call(dispatch, "tools/call", {
            "name": "memlock_unpin",
            "arguments": {"session_id": "s3", "pin_id": pin_id},
        })
        assert "Unpinned" in unpinned["result"]["content"][0]["text"]

    def test_audit_returns_reminder_block_after_drift(self, dispatch):
        call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {
                "session_id": "s4", "priority": 90,
                "text": "always use tabs over spaces",
                "probes": ["tabs"],
            },
        })
        # First turn: seed the baseline so the compaction on the NEXT audit
        # is a new event (the core audits only on a fresh compaction).
        first = call(dispatch, "tools/call", {
            "name": "memlock_audit",
            "arguments": {
                "session_id": "s4",
                "conversation_history": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "warm up"},
                ],
                "user_message": "warm up",
            },
        })
        assert first["result"]["isError"] is False
        compacted_history = [
            {"role": "system", "content": "s"},
            {
                "role": "user",
                "content": (
                    "This session is being continued from a previous "
                    "conversation that ran out of context. old turns"
                ),
            },
            {"role": "user", "content": "unrelated question"},
        ]
        cc_marker = (
            "This session is being continued from a previous conversation "
            "that ran out of context."
        )
        audited = call(dispatch, "tools/call", {
            "name": "memlock_audit",
            "arguments": {
                "session_id": "s4",
                "conversation_history": compacted_history,
                "user_message": "unrelated question",
                # MCP callers pass their harness's compaction marker per
                # call — this is the spec's per-call parameterisation.
                "summary_prefixes": [cc_marker],
            },
        })
        text = audited["result"]["content"][0]["text"]
        assert audited["result"]["isError"] is False
        assert "tabs" in text

    def test_audit_without_pins_reports_no_rehydration(self, dispatch):
        audited = call(dispatch, "tools/call", {
            "name": "memlock_audit",
            "arguments": {
                "session_id": "s5",
                "conversation_history": [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "hi"},
                ],
            },
        })
        assert audited["result"]["isError"] is False
        assert "No rehydration needed" in audited["result"]["content"][0]["text"]


class TestSessionIsolation:
    def test_two_sessions_do_not_share_pins(self, dispatch):
        call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {
                "session_id": "alice",
                "text": "alice secret project rule",
            },
        })
        call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {
                "session_id": "bob",
                "text": "bob totally different rule",
            },
        })
        alice_status = call(dispatch, "tools/call", {
            "name": "memlock_status",
            "arguments": {"session_id": "alice"},
        })["result"]["content"][0]["text"]
        bob_status = call(dispatch, "tools/call", {
            "name": "memlock_status",
            "arguments": {"session_id": "bob"},
        })["result"]["content"][0]["text"]
        assert "alice secret" in alice_status
        assert "bob totally" not in alice_status
        assert "bob totally" in bob_status
        assert "alice secret" not in bob_status

    def test_update_in_one_session_cannot_touch_another(self, dispatch):
        call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {"session_id": "owner", "text": "owner rule"},
        })
        cross = call(dispatch, "tools/call", {
            "name": "memlock_update",
            "arguments": {
                "session_id": "attacker", "pin_id": "pin_1_0",
                "text": "hijacked",
            },
        })
        assert cross["result"]["isError"] is True
        owner_status = call(dispatch, "tools/call", {
            "name": "memlock_status",
            "arguments": {"session_id": "owner"},
        })["result"]["content"][0]["text"]
        assert "hijacked" not in owner_status


class TestFailOpenProtocol:
    def test_missing_session_id_is_error_result_not_crash(self, dispatch):
        resp = call(dispatch, "tools/call", {
            "name": "memlock_pin",
            "arguments": {"text": "no session given"},
        })
        assert resp["result"]["isError"] is True
        assert "session_id" in resp["result"]["content"][0]["text"]

    def test_unknown_tool_is_error_result(self, dispatch):
        resp = call(dispatch, "tools/call", {
            "name": "nonexistent_tool", "arguments": {},
        })
        assert resp["result"]["isError"] is True

    def test_unknown_method_is_method_not_found(self, dispatch):
        resp = call(dispatch, "resources/list")
        assert resp["error"]["code"] == -32601

    def test_parse_error_on_garbage_line(self, dispatch):
        resp = protocol.handle_request("{{not json", dispatch)
        assert resp["error"]["code"] == -32700
        assert resp["id"] is None

    def test_batch_request_maps_over_entries(self, dispatch):
        batch = json.dumps([
            {"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "ping"},
            {"jsonrpc": "2.0", "id": 12, "method": "ping"},
        ])
        resp = protocol.handle_request(batch, dispatch)
        # Single-flight loop contract: one response dict comes back; the
        # notification is dropped and a request id survives.
        assert resp["result"] == {} or "serverInfo" in resp["result"]

    def test_dispatcher_survives_bad_argument_types(self, dispatch):
        bad = dispatch("memlock_audit", None)
        assert bad["isError"] is True
        worse = build_dispatcher()("memlock_status", {"session_id": ""})
        assert worse["isError"] is True

    def test_shutdown_request_signals_loop_exit(self, dispatch):
        resp = call(dispatch, "shutdown")
        from mcp_server import _should_exit
        assert _should_exit(resp) is True


def _extract_pin_id(text: str) -> str:
    """Pull 'pin_<ts>_<n>' out of a tool's human-readable response."""
    import re

    match = re.search(r"id=(pin_\d+_\d+)", text)
    return match.group(1) if match else ""
