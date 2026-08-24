"""M4 regression test: server pipelines notification + tools/call response in
ONE stdout chunk behind the initialize response => buffered reader still
delivers every response; nothing is lost between handshake and call.

Server is compliant: echoes each request's id, writes the notification and
the tools/call response as a single write (single pipe chunk) so only a
buffer that carries remainder bytes across reads can parse both.
"""

import json
import sys

import memlock_adapters.mcp_provider as mc


def test_buffered_reader_carries_remainder(tmp_path):
    server_script = (
        "import json, sys, time\n"
        "# 1) initialize -> echo result with true id\n"
        "init = sys.stdin.readline()\n"
        "msg = json.loads(init)\n"
        'sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {\n'
        '    "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},\n'
        '    "serverInfo": {"name": "test-server", "version": "1.0.0"}}}) + chr(10))\n'
        "sys.stdout.flush()\n"
        "# 2) read notifications/initialized AND the pending tools/call request,\n"
        "#    then answer BOTH in one write: notification line + call response line.\n"
        "n1 = sys.stdin.readline()\n"
        "call_req = sys.stdin.readline()\n"
        'call_id = json.loads(call_req).get("id", 2)\n'
        'notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}\n'
        'call_response = {"jsonrpc": "2.0", "id": call_id, "result": {\n'
        '    "content": [{"type": "text",\n'
        '                 "text": \'[{"id": "test-pref", "content": "test preference"}]\'}],\n'
        '    "isError": False}}\n'
        "# ONE write => one pipe chunk: notification newline + full response + newline\n"
        "sys.stdout.write(json.dumps(notification) + chr(10) + json.dumps(call_response) + chr(10))\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.2)\n"
    )
    server_path = tmp_path / "pipeline_server.py"
    server_path.write_text(server_script)

    provider = mc.make_provider(
        mcp_command=[sys.executable, str(server_path)],
        mcp_tool="test-tool",
    )

    rows = provider("test query", 5)

    # The pipelined chunk must have been fully parsed despite arriving as
    # one write behind the initialize response (M4).
    assert len(rows) == 1
    assert rows[0]["id"] == "test-pref"
    assert rows[0]["content"] == "test preference"
