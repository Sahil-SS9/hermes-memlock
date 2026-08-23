"""Minimal MCP protocol surface — JSON-RPC 2.0 request/response dicts.

Implements exactly what a MemLock sidecar needs: ``initialize``,
``notifications/initialized``, ``ping``, ``tools/list``, ``tools/call`` and
``shutdown``. Pure dict-in/dict-out: :func:`handle_request` takes one raw
JSON line and the dispatcher, and returns the response dict (or None for
notifications). The stdio loop in the package ``__init__`` is the only
caller that touches real streams.

Batched requests are accepted (JSON-RPC 2.0 §6) by mapping over entries.
"""
from __future__ import annotations

import json
import logging

from .tools import SERVER_NAME, SERVER_VERSION

logger = logging.getLogger(__name__)

JSONRPC = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _ok(req_id: object | None, result: dict) -> dict:
    return {"jsonrpc": JSONRPC, "id": req_id, "result": result}


def _err(req_id: object | None, code: int, message: str) -> dict:
    return {
        "jsonrpc": JSONRPC, "id": req_id,
        "error": {"code": code, "message": message},
    }


def server_info() -> dict:
    """The initialize result: protocol revision + capabilities + identity."""
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def handle_request(raw_line: str, dispatch) -> dict | None:
    """Handle one raw JSON-RPC line; returns the response dict or None.

    Notifications produce no response. Malformed lines produce a Parse Error
    with id=null. Anything unexpected inside a known method becomes an
    INVALID_PARAMS error rather than an exception — fail-open at the wire.
    """
    try:
        message = json.loads(raw_line)
    except (TypeError, ValueError):
        return _err(None, PARSE_ERROR, "Parse error")

    if isinstance(message, list):
        responses = [
            r for r in (_handle_one(m, dispatch) for m in message)
            if r is not None
        ]
        return responses[0] if responses else None  # single-flight loop
    return _handle_one(message, dispatch)


def _handle_one(message: object, dispatch) -> dict | None:
    if not isinstance(message, dict):
        return _err(None, INVALID_REQUEST, "Invalid Request")
    req_id = message.get("id")
    method = str(message.get("method", "") or "")

    # A notification carries no id; it must never produce a response.
    is_notification = "id" not in message

    if method == "notifications/initialized":
        return None

    if method == "initialize":
        return _ok(req_id, server_info())

    if method == "ping":
        if is_notification:
            return None
        return _ok(req_id, {})

    if method == "shutdown":
        if is_notification:
            return None
        return _ok(req_id, {"_memlock_shutdown": True})

    if method == "tools/list":
        if is_notification:
            return None
        try:
            return _ok(req_id, {"tools": dispatch.list_tools()})
        except Exception as exc:
            logger.warning("memlock-mcp: tools/list failed: %s", exc)
            return _err(req_id, INVALID_PARAMS, str(exc))

    if method == "tools/call":
        if is_notification:
            return None
        params = message.get("params")
        if not isinstance(params, dict):
            return _err(req_id, INVALID_PARAMS, "params must be an object")
        name = str(params.get("name", "") or "")
        arguments = params.get("arguments")
        try:
            result = dispatch(name, arguments)
        except Exception as exc:  # defensive: dispatcher already fails open
            logger.warning("memlock-mcp: tools/call crashed fail-open: %s", exc)
            result = {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            }
        return _ok(req_id, result)

    if is_notification:
        return None
    return _err(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")
