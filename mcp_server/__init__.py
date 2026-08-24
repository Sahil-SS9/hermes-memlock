"""MemLock MCP server — stdio JSON-RPC 2.0, zero dependencies.

Exposes MemLock's pin lifecycle to ANY harness that speaks the Model Context
Protocol (stdio transport): initialize handshake, tools/list, tools/call.

Layering (what makes this testable in-process):

  - :mod:`tools`    — pure ``dispatch(tool_name, arguments) -> dict`` over a
                      MemlockService. No I/O, no protocol. Tests call it
                      directly with fabricated arguments.
  - :mod:`protocol` — the minimal MCP/JSON-RPC 2.0 surface: request →
                      response dicts. Also pure; tests drive round-trips.
  - ``main()``      — the ONLY process-touching part: reads stdin lines,
                      writes stdout lines. Swappable transport, nothing else.

Session identity: every tool takes a required caller-supplied ``session_id``
argument (spec: isolation model identical to the plugin's). Fail-open
convention: handler errors become JSON-RPC error responses or per-tool
``isError`` results — the server loop never dies on one bad call.
"""
from __future__ import annotations

import sys

from .protocol import handle_request
from .tools import build_dispatcher


def main() -> int:
    """Stdio transport loop: one JSON-RPC request per stdin line.

    Reads via select with a generous idle timeout (L1) so a wedged
    dispatcher or a half-dead parent cannot block this process forever:
    on idle expiry the loop exits cleanly rather than hanging.
    """
    import select

    dispatch = build_dispatcher()
    stdin = sys.stdin
    idle_timeout_s = 3600.0  # one hour of silence => clean exit
    while True:
        ready, _, _ = select.select([stdin], [], [], idle_timeout_s)
        if not ready:
            # Parent went quiet without closing stdin (crashed client).
            break
        line = stdin.readline()
        if not line:  # EOF — parent closed stdin, normal shutdown
            break
        line = line.strip()
        if not line:
            continue
        try:
            response = handle_request(line, dispatch)
        except Exception as exc:  # belt-and-braces: loop must survive
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is not None:
            import json as _json

            sys.stdout.write(_json.dumps(response) + "\n")
            sys.stdout.flush()
        if _should_exit(response):
            break
    return 0


def _should_exit(response: dict | None) -> bool:
    """Exit after a successful 'shutdown' request (MCP lifecycle)."""
    if response is None or "result" not in response:
        return False
    return isinstance(response.get("result"), dict) and \
        response["result"].get("_memlock_shutdown") is True


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess smoke
    raise SystemExit(main())
