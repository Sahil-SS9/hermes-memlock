"""MCP-first adapter tests — no real network, no real provider process.

The adapter is exercised against a small stdlib fake that speaks minimal
MCP over stdio (newline-delimited JSON-RPC, the same framing the repo's
own mcp_server/ serves): initialize handshake, tools/call, canned
preference rows. The suite points ``mcp_command`` at
``[sys.executable, <fake script>]`` so every test spawns a real child —
which is exactly what lets us assert zombie-free teardown and fail-open
behaviour on genuine crashes, timeouts and protocol garbage.
"""
from __future__ import annotations

import json
import sys

import pytest

import memlock_adapters as ad
from memlock_adapters import mcp_provider as mc


# ── the fake MCP server ───────────────────────────────────────────────────

FAKE_SERVER = r'''
"""Minimal MCP stdio server: initialize + tools/call returning canned rows.

Behaviour knobs come via argv:
  argv[1] = mode   one of ok, crash, hang, garbage, unknown_tool,
                   tool_error, empty, nonjson_rows, structured
  argv[2] = query echo check marker (unused by the server itself)
"""
import json
import os
import signal
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"

ROWS = [
    {
        "id": "pref-1",
        "content": "User prefers bullet points in every reply",
        "source": "user-stated",
        "created_at": "2026-08-01T10:00:00Z",
        "scope": "global",
        "veracity": "confirmed",
        "memory_type": "preference",
        "metadata_json": json.dumps({"memlock": {"priority": 90}}),
    },
    {
        "id": "pref-2",
        "content": "Always write British English spellings",
        "timestamp": "2026-07-15T09:30:00Z",
    },
]


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def serve():
    while True:
        line = sys.stdin.readline()
        if not line:
            return
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method", "")
        req_id = msg.get("id")
        if method == "initialize":
            emit({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-mem-provider",
                                   "version": "1.0.0"},
                },
            })
        elif method == "notifications/initialized":
            pass  # notification: no response
        elif method == "ping":
            if req_id is not None:
                emit({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif method == "shutdown":
            if req_id is not None:
                emit({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return  # cooperative exit on EOF anyway; be prompt
        elif method == "tools/call":
            name = str((msg.get("params") or {}).get("name", ""))
            if mode == "unknown_tool":
                emit({"jsonrpc": "2.0", "id": req_id, "error":
                      {"code": -32602, "message": f"Unknown tool: {name}"}})
            elif mode == "tool_error":
                emit({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text",
                                     "text": "backend exploded"}],
                        "isError": True,
                    },
                })
            elif mode == "empty":
                emit({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": "[]"}]},
                })
            elif mode == "structured":
                # MCP 2025-06-18 style: rows in structuredContent only.
                emit({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "ok"}],
                        "structuredContent": {"rows": ROWS},
                    },
                })
            else:  # ok / garbage / nonjson_rows all answer from here
                payload = list(ROWS)
                if mode == "nonjson_rows":
                    payload = [
                        ROWS[0],
                        {"garbage": True},
                        {"id": "", "content": "no id"},
                        {"id": "skip-me"},          # no content
                        "a bare string row",
                        ROWS[1],
                    ]
                text = json.dumps(payload)
                if mode == "garbage":
                    text = (
                        'NOT JSON AT ALL\n' + text +
                        '\n{"id": "trailing", "content": "row"}'
                    )
                emit({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": text}]},
                })
        else:
            if req_id is not None:
                emit({"jsonrpc": "2.0", "id": req_id, "error":
                      {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    if mode == "crash":
        print("this is not json at all", flush=True)  # then die mid-protocol
        raise SystemExit(7)
    if mode == "hang":
        # Never answers initialize until SIGKILLed by teardown.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(60)
    serve()
'''


@pytest.fixture
def fake_server(tmp_path):
    """Write the fake MCP server script; return its path."""
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER)
    return str(path)


def make(fake_server, mode, **kwargs):
    """Provider bound to this test's fake server running ``mode``."""
    kwargs.setdefault("mcp_command", [sys.executable, fake_server, mode])
    return mc.make_provider(**kwargs)


# ── happy paths ───────────────────────────────────────────────────────────


def test_happy_path_maps_rows_to_preference_memory_shape(fake_server):
    provider = make(fake_server, "ok")
    rows = provider("applicable user preferences", 25)

    assert [r["id"] for r in rows] == ["pref-1", "pref-2"]
    first = rows[0]
    # Full detection.PreferenceMemory column set present, optional → None.
    assert first["content"] == "User prefers bullet points in every reply"
    assert first["source"] == "user-stated"
    assert first["created_at"] == "2026-08-01T10:00:00Z"
    assert first["valid_until"] is None
    assert first["superseded_by"] is None
    assert first["scope"] == "global"
    assert first["veracity"] == "confirmed"
    assert first["memory_type"] == "preference"
    assert json.loads(first["metadata_json"]) == {"memlock": {"priority": 90}}
    second = rows[1]
    assert second["timestamp"] == "2026-07-15T09:30:00Z"
    assert second["memory_type"] is None


def test_query_and_limit_forwarded_as_tool_arguments(fake_server, tmp_path):
    seen_path = tmp_path / "seen.json"
    server = tmp_path / "echo_server.py"
    base = FAKE_SERVER
    # Wrap tools/call so arguments are recorded before answering normally.
    wrapped = base.replace(
        'elif method == "tools/call":\n'
        '            name = str((msg.get("params") or {}).get("name", ""))',
        'elif method == "tools/call":\n'
        '            name = str((msg.get("params") or {}).get("name", ""))\n'
        '            open(r"@@PATH@@", "w").write(json.dumps('
        '(msg.get("params") or {}).get("arguments")))',
    ).replace("@@PATH@@", str(seen_path).replace("\\", "\\\\"))
    assert wrapped != base, "echo wrapper must have been injected"
    server.write_text(wrapped)

    provider = mc.make_provider(
        mcp_command=[sys.executable, str(server)], mcp_tool="search"
    )
    provider("find my formatting preferences", 7)

    seen = json.loads(seen_path.read_text())
    assert seen == {"query": "find my formatting preferences", "limit": 7}


def test_structured_content_rows_accepted(fake_server):
    """MCP structuredContent-only servers work too."""
    provider = make(fake_server, "structured")
    rows = provider("q", 10)
    assert [r["id"] for r in rows] == ["pref-1", "pref-2"]


# ── malformed payloads ────────────────────────────────────────────────────


def test_nonjson_and_malformed_rows_skipped_batch_survives(fake_server):
    """Malformed candidates inside a JSON row list are skipped, not fatal.

    The fake mixes five unusable entries (dict without id/content, empty
    id, missing content, bare string) around the two good rows; the batch
    must keep exactly the usable ones.
    """
    provider = make(fake_server, "nonjson_rows")
    rows = provider("q", 20)
    assert [r["id"] for r in rows] == ["pref-1", "pref-2"]


def test_garbage_around_json_array_still_yields_valid_rows(fake_server):
    provider = make(fake_server, "garbage")
    rows = provider("q", 10)
    assert [r["id"] for r in rows] == ["pref-1", "pref-2", "trailing"]


# ── RPC / tool errors ─────────────────────────────────────────────────────


def test_unknown_tool_name_fails_open_with_warning(fake_server, caplog):
    provider = make(fake_server, "unknown_tool", mcp_tool="no_such_verb")
    with caplog.at_level("WARNING", logger="memlock_adapters.mcp_provider"):
        rows = provider("q", 5)
    assert rows == []
    assert any("tools/call refused" in rec.message for rec in caplog.records)


def test_tool_isError_result_fails_open(fake_server, caplog):
    provider = make(fake_server, "tool_error")
    with caplog.at_level("WARNING", logger="memlock_adapters.mcp_provider"):
        rows = provider("q", 5)
    assert rows == []
    assert any("provider tool error" in rec.message for rec in caplog.records)


def test_empty_payload_from_known_tool_returns_empty_list(fake_server):
    provider = make(fake_server, "empty")
    rows = provider("q", 5)
    # Server answered fine but offered zero rows: an empty result, not an
    # error path... but extract_rows treats it as unusable → fail-open [].
    assert rows == []


# ── spawn / crash / timeout ───────────────────────────────────────────────


def test_spawn_failure_fails_open():
    provider = mc.make_provider(mcp_command=["/nonexistent-binary-xyz"])
    assert provider("q", 5) == []


def test_no_command_configured_fails_open(caplog, monkeypatch):
    monkeypatch.delenv("MEMLOCK_MCP_COMMAND", raising=False)
    with caplog.at_level("WARNING", logger="memlock_adapters.mcp_provider"):
        rows = mc.make_provider()("q", 5)
    assert rows == []
    assert any("no mcp_command" in rec.message for rec in caplog.records)


def test_child_crash_midprotocol_fails_open(fake_server, caplog):
    provider = make(fake_server, "crash")
    with caplog.at_level("WARNING", logger="memlock_adapters.mcp_provider"):
        rows = provider("q", 5)
    assert rows == []
    assert any("fail-open" in rec.message for rec in caplog.records)


def test_unresponsive_child_times_out_fail_open(fake_server):
    """A hung provider must hit the deadline, not wedge the audit."""
    provider = make(fake_server, "hang", mcp_timeout_s=1.0)
    assert provider("q", 5) == []  # returns within ~timeout, not 60s


# ── config resolution ─────────────────────────────────────────────────────


def test_mcp_url_config_refuses_explicitly(fake_server, caplog):
    provider = mc.make_provider(mcp_command=None, mcp_url="http://x:9000/mcp")
    with caplog.at_level("WARNING", logger="memlock_adapters.mcp_provider"):
        rows = provider("q", 5)
    assert rows == []
    assert any(
        "HTTP transport" in rec.message and "not implemented" in rec.message
        for rec in caplog.records
    )


def test_command_resolution_accepts_string_json_and_argv(monkeypatch):
    monkeypatch.delenv("MEMLOCK_MCP_COMMAND", raising=False)
    assert mc._resolve_command(["a", "b"]) == ["a", "b"]
    assert mc._resolve_command(("a",)) == ["a"]
    assert mc._resolve_command('["python", "-u", "srv.py"]') == [
        "python", "-u", "srv.py",
    ]
    assert mc._resolve_command('python -u "my dir/srv.py"') == [
        "python", "-u", "my dir/srv.py",
    ]
    assert mc._resolve_command(None) == []
    assert mc._resolve_command("") == []
    monkeypatch.setenv("MEMLOCK_MCP_COMMAND", '["py", "x.py"]')
    assert mc._resolve_command(None) == ["py", "x.py"]  # env fallback wins


def test_tool_and_timeout_defaults_and_env(monkeypatch):
    monkeypatch.delenv("MEMLOCK_MCP_TOOL", raising=False)
    monkeypatch.delenv("MEMLOCK_MCP_TIMEOUT_S", raising=False)
    assert mc._resolve_tool(None) == "search"
    assert mc._resolve_timeout(None) == 10.0
    assert mc._resolve_timeout(-3) == 0.5  # clamped floor
    assert mc._resolve_timeout("junk") == 10.0
    monkeypatch.setenv("MEMLOCK_MCP_TOOL", "mem.search")
    monkeypatch.setenv("MEMLOCK_MCP_TIMEOUT_S", "3")
    assert mc._resolve_tool(None) == "mem.search"
    assert mc._resolve_timeout(None) == 3.0


def test_registry_exposes_mcp_adapter():
    assert "mcp" in ad.available_adapters()
    provider = ad.get_provider("mcp")
    # Unconfigured (no command/env) it fails open to [] rather than None:
    # the factory succeeded, the runtime just has nothing to talk to.
    assert callable(provider)
    assert provider("q", 5) == []


# ── zombie hygiene ────────────────────────────────────────────────────────
#
# /proc walks instead of `ps`: any listing helper would itself be a child
# of this process and could list itself (or race its own exit), turning a
# deterministic assertion into a flaky one. Reading /proc/<pid>/stat adds
# zero processes and cannot self-report.


def _parent_pid() -> int:
    """This test process's parent, straight from /proc (no fork needed)."""
    with open("/proc/self/stat", "rb") as fh:
        data = fh.read()
    # comm may contain spaces/parens; ppid is field 4 after the last ')'.
    fields = data[data.rindex(b")") + 2:].split()
    return int(fields[1])


def _child_pids(pid: int) -> set[int]:
    """Live direct children of ``pid`` from the kernel's own child list."""
    try:
        with open(f"/proc/{pid}/task/{pid}/children") as fh:
            return {int(x) for x in fh.read().split()}
    except OSError:
        return set()


def _stat_state(pid: int) -> str:
    """Process state letter (R/S/Z/...) from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return ""
    # comm may contain spaces/parens; state follows the last ')'.
    try:
        return data[data.rindex(b")") + 2:data.rindex(b")") + 3].decode()
    except (ValueError, IndexError):  # pragma: no cover
        return ""


@pytest.mark.parametrize("mode", ["ok", "crash", "hang"])
def test_no_zombie_children_after_query(fake_server, mode):
    """Every spawned fake provider is reaped — even after failure paths.

    Asserts BOTH halves of zombie hygiene: no child left in Z state (the
    kernel still tracks it) and no child at all (the hang mode must have
    been killed, not merely orphaned to live on behind the audit).
    """
    cmd = [sys.executable, fake_server, mode]
    provider = mc.make_provider(mcp_command=cmd, mcp_timeout_s=1.0)
    provider("q", 5)  # ok: success path; crash/hang: fail-open paths

    children = _child_pids(_parent_pid())
    zombies = {p for p in children if _stat_state(p) == "Z"}
    assert zombies == set(), f"zombie children left behind: {zombies}"
    assert children == set(), f"live children outlived the query: {children}"
