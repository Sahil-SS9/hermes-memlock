"""M5 regression test: batched JSON-RPC returns array of responses."""

import json

import pytest

from mcp_server import protocol
from mcp_server.tools import build_dispatcher


def test_batch_request_returns_array_of_responses():
    """M5: batch of 3 requests returns array of 3 responses."""
    dispatch = build_dispatcher()
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "ping"},  # notification
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ])
    resp = protocol.handle_request(batch, dispatch)
    # Batch returns list of responses (notifications dropped)
    assert isinstance(resp, list) and len(resp) == 2
    # First response: initialize result
    assert resp[0]["id"] == 1
    assert "result" in resp[0]
    assert "serverInfo" in resp[0]["result"]
    # Second response: ping result
    assert resp[1]["id"] == 3
    assert resp[1]["result"] == {}