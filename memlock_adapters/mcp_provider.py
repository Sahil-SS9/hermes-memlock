"""Generic MCP-first adapter — queries an EXTERNAL memory provider over MCP.

WHY MCP-first: every bespoke adapter couples MemLock to one provider's
storage engine. A provider that speaks Model Context Protocol exposes a
search verb over a standard JSON-RPC surface, so ONE read-only client
covers mem0/OpenMemory, Letta, Zep and anything else MCP-shaped — without
MemLock importing a single named-provider SDK. The native direct adapters
(severian.py, mnemosyne.py) stay as zero-dependency fast paths.

Transport: stdio only today. The child is launched from ``mcp_command``
and spoken to as newline-delimited JSON-RPC 2.0 — the exact framing this
repo's own ``mcp_server/`` serves and the de-facto MCP stdio convention.
An ``mcp_url`` (HTTP transport) is accepted in config for forward
compatibility but deliberately refuses with a warning rather than
half-working: an explicit, visible gap beats a silently wrong one.

Per-query flow: spawn → ``initialize`` handshake →
``notifications/initialized`` → ``tools/call`` {query, limit} → parse
preference rows out of the result content → best-effort ``shutdown`` →
close stdin → wait, escalate to kill, and ALWAYS reap (wait again) so a
failing provider can never leave a zombie behind the pre_llm_call hook.

Fail-open contract (mirrors the other adapters): spawn failure, protocol
timeout, dead child, non-JSON payloads, RPC/tool errors and unusable rows
each log one warning and return []. Malformed rows are skipped rather
than aborting the batch. Stdlib-only: subprocess/json/selectors/shlex —
nothing to import lazily, nothing to install.
"""
from __future__ import annotations

import json
import logging
import os
import selectors
import shlex
import subprocess
import time

logger = logging.getLogger(__name__)

# JSON-RPC request ids for the three calls this client makes per query.
_INIT_ID = 1
_CALL_ID = 2
_SHUTDOWN_ID = 3

_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "memlock-reverse-audit", "version": "0.5.0"}

DEFAULT_MCP_TOOL = "search"
DEFAULT_TIMEOUT_S = 10.0
_MIN_TIMEOUT_S = 0.5  # floor so a misconfigured 0/negative can't busy-spin

# Env fallbacks, matching the SEVERIAN_DSN / MNEMOSYNE_* convention:
# zero-argument registry factories discover their config from the env.
_ENV_COMMAND = "MEMLOCK_MCP_COMMAND"
_ENV_URL = "MEMLOCK_MCP_URL"
_ENV_TOOL = "MEMLOCK_MCP_TOOL"
_ENV_TIMEOUT = "MEMLOCK_MCP_TIMEOUT_S"

# Keys scanned (in order) when a tools/call result wraps its row list in
# an object instead of returning a bare JSON array.
_ROW_LIST_KEYS = ("rows", "memories", "results", "preferences", "items", "data")


class _ProviderFailure(Exception):
    """Internal: any protocol/payload condition that fails open to []."""


def _resolve_command(override: object) -> list[str]:
    """mcp_command kwarg wins, then MEMLOCK_MCP_COMMAND; [] = unconfigured.

    Accepts a list/tuple of argv parts, a JSON array string, or a plain
    shell-ish string (shlex-split so quoted paths survive).
    """
    raw: object = override
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raw = os.environ.get(_ENV_COMMAND, "")
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw if str(part).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, list):
        return [str(part) for part in parsed if str(part).strip()]
    return shlex.split(text)


def _resolve_url(override: object) -> str:
    """mcp_url kwarg wins, then MEMLOCK_MCP_URL."""
    raw = override
    if raw is None or not str(raw).strip():
        raw = os.environ.get(_ENV_URL, "")
    return str(raw or "").strip()


def _resolve_tool(override: object) -> str:
    """mcp_tool kwarg wins, then MEMLOCK_MCP_TOOL, then 'search'."""
    raw = override
    if raw is None or not str(raw).strip():
        raw = os.environ.get(_ENV_TOOL, "")
    return str(raw or "").strip() or DEFAULT_MCP_TOOL


def _resolve_timeout(override: object) -> float:
    """mcp_timeout_s kwarg wins, then MEMLOCK_MCP_TIMEOUT_S, clamped ≥ 0.5."""
    raw = override
    if raw is None or str(raw).strip() == "":
        raw = os.environ.get(_ENV_TIMEOUT, "")
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(_MIN_TIMEOUT_S, value)


# ── wire plumbing ─────────────────────────────────────────────────────────


def _send(proc: subprocess.Popen, payload: dict) -> None:
    """Write one JSON-RPC line to the child; dead pipe → _ProviderFailure."""
    stdin = proc.stdin
    if stdin is None:
        raise _ProviderFailure("child stdin unavailable")
    try:
        stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
        stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise _ProviderFailure(f"failed writing to MCP provider: {exc}") from exc


def _read_line(proc: subprocess.Popen, deadline: float) -> str:
    """Read one newline-terminated response line, honouring the deadline.

    Uses a selector on the raw fd so a wedged provider cannot block the
    audit past the timeout. Platforms whose selector cannot watch pipes
    degrade to a blocking readline (timeout unenforced there) rather than
    refusing to work at all.
    """
    stdout = proc.stdout
    if stdout is None:
        raise _ProviderFailure("child stdout unavailable")
    sel: selectors.BaseSelector | None = None
    try:
        sel = selectors.DefaultSelector()
        sel.register(stdout, selectors.EVENT_READ)
    except (OSError, ValueError):
        if sel is not None:
            sel.close()
        sel = None
    buf = bytearray()
    try:
        while True:
            if sel is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("timed out waiting for MCP provider")
                if not sel.select(remaining):
                    raise TimeoutError("timed out waiting for MCP provider")
                chunk = os.read(stdout.fileno(), 65536)
            else:  # pragma: no cover - non-pipe-capable platforms
                chunk = stdout.readline()
            if not chunk:
                raise _ProviderFailure("MCP provider closed the connection")
            buf.extend(chunk)
            if b"\n" in buf:
                break
    finally:
        if sel is not None:
            sel.close()
    line, _, _rest = bytes(buf).partition(b"\n")
    return line.decode("utf-8", errors="replace")


def _request(proc: subprocess.Popen, payload: dict, req_id: int,
             deadline: float) -> dict:
    """Send a request and return its matching response object.

    Notifications, server-initiated messages and undecodable lines are
    skipped; only the reply carrying ``req_id`` counts. EOF/timeout raise.
    """
    _send(proc, payload)
    while True:
        line = _read_line(proc, deadline)
        try:
            message = json.loads(line)
        except ValueError:
            continue  # tolerate chatty garbage between real responses
        if isinstance(message, dict) and message.get("id") == req_id:
            return message


def _handshake(proc: subprocess.Popen, deadline: float) -> None:
    """initialize → notifications/initialized, validating the result."""
    response = _request(
        proc,
        {
            "jsonrpc": "2.0", "id": _INIT_ID, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        },
        _INIT_ID, deadline,
    )
    if "error" in response:
        raise _ProviderFailure(
            f"initialize refused: {response['error']}"
        )
    if not isinstance(response.get("result"), dict):
        raise _ProviderFailure("initialize returned no result object")
    # Notification: no id, no response expected.
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})


# ── result parsing ────────────────────────────────────────────────────────


def _decoded(candidate: object) -> list[object]:
    """JSON-decode one candidate payload piece into decoded documents.

    Whole-payload parse first; on failure, tolerate chatty servers by
    scanning newline-by-newline for embedded JSON documents (servers that
    ignore the "stdout is protocol-only" rule are common in the wild).
    Returns [] when nothing decodes.
    """
    if isinstance(candidate, (dict, list)):
        return [candidate]
    if isinstance(candidate, (str, bytes, bytearray)):
        try:
            return [json.loads(candidate)]
        except (TypeError, ValueError):
            pass
        if isinstance(candidate, bytes):
            candidate = candidate.decode("utf-8", errors="replace")
        pieces: list[object] = []
        for line in str(candidate).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pieces.append(json.loads(line))
            except ValueError:
                continue
        return pieces
    return []


def _rows_from_decoded(decoded: object, rows: list[object]) -> None:
    """Collect row-shaped objects from one decoded payload piece."""
    if isinstance(decoded, list):
        rows.extend(decoded)
        return
    if not isinstance(decoded, dict):
        return
    if "id" in decoded or "content" in decoded:
        rows.append(decoded)  # a single row, bare
        return
    for key in _ROW_LIST_KEYS:
        wrapped = decoded.get(key)
        if isinstance(wrapped, list):
            rows.extend(wrapped)
            return


def extract_rows(result: object) -> list[object]:
    """Pull preference-row candidates out of a tools/call result.

    Accepts the shapes seen across real MCP servers: a bare JSON array in
    text content, a single row object, or an object wrapping the list
    under rows/memories/results/preferences/items/data (including MCP
    2025-style structuredContent). Raises _ProviderFailure when nothing
    usable is found — the caller turns that into warning + [].
    """
    if not isinstance(result, dict):
        raise _ProviderFailure("tools/call returned no result object")
    if result.get("isError"):
        text = ""
        content = result.get("content")
        if isinstance(content, list) and content:
            first = content[0]
            if isinstance(first, dict):
                text = str(first.get("text", "") or "")
        raise _ProviderFailure(f"provider tool error: {text or 'unknown'}")

    candidates: list[object] = []
    structured = result.get("structuredContent")
    if structured is not None:
        candidates.append(structured)
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                candidates.append(item.get("text"))

    rows: list[object] = []
    saw_decoded_list = False
    for candidate in candidates:
        for decoded in _decoded(candidate):
            if isinstance(decoded, list):
                # An explicitly empty list is a legitimate "no preferences"
                # answer, not a malformed payload: honour it sans warning.
                saw_decoded_list = True
            _rows_from_decoded(decoded, rows)
    if not rows and not saw_decoded_list:
        raise _ProviderFailure("unusable tools/call payload (no JSON rows)")
    return rows


# ── teardown ──────────────────────────────────────────────────────────────


def _teardown(proc: subprocess.Popen | None, grace_s: float = 2.0) -> None:
    """Best-effort clean stop; ALWAYS reap so no zombie survives a query.

    Order matters: polite ``shutdown`` request → close stdin (cooperative
    servers exit on EOF) → bounded wait → kill → wait again. Every step
    tolerates an already-dead child; the final wait is unconditional.
    """
    if proc is None:
        return
    try:
        _send(proc, {
            "jsonrpc": "2.0", "id": _SHUTDOWN_ID, "method": "shutdown",
        })
    except Exception:
        pass  # already dead / never initialised — teardown continues
    for stream in (proc.stdin, proc.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=grace_s)
    except Exception:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace_s)
        except Exception as exc:
            logger.warning(
                "memlock: mcp provider could not be reaped: %s", exc,
            )


# ── provider construction ─────────────────────────────────────────────────


def make_provider(
    mcp_command: object = None,
    mcp_url: object = None,
    mcp_tool: object = None,
    mcp_timeout_s: object = None,
):
    """Build the preference provider callable for a generic MCP server.

    All arguments are optional config overrides (the memlock config
    section's ``mcp_*`` keys); unset keys fall back to their MEMLOCK_MCP_*
    environment variables and then to documented defaults.
    """

    def get_preference_provider(query: str, limit: int) -> list[dict]:
        from memlock_adapters import normalise_row  # local: avoids cycles

        command = _resolve_command(mcp_command)
        url = _resolve_url(mcp_url)
        if not command:
            if url:
                # Explicit, visible refusal: HTTP transport is future work.
                logger.warning(
                    "memlock: mcp_url %s configured but the HTTP transport "
                    "is not implemented yet; use mcp_command (stdio). "
                    "Returning no rows",
                    url,
                )
            else:
                logger.warning(
                    "memlock: mcp adapter has no mcp_command configured; "
                    "returning no rows"
                )
            return []

        tool = _resolve_tool(mcp_tool)
        timeout = _resolve_timeout(mcp_timeout_s)
        deadline = time.monotonic() + timeout
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # provider logs can't deadlock us
            )
        except OSError as exc:
            logger.warning(
                "memlock: mcp provider spawn failed (fail-open): %s", exc,
            )
            return []

        raw_rows: list[object] = []
        try:
            _handshake(proc, deadline)
            response = _request(
                proc,
                {
                    "jsonrpc": "2.0", "id": _CALL_ID, "method": "tools/call",
                    "params": {
                        "name": tool,
                        "arguments": {
                            "query": str(query or ""),
                            "limit": max(1, int(limit)),
                        },
                    },
                },
                _CALL_ID, deadline,
            )
            if "error" in response:
                raise _ProviderFailure(
                    f"tools/call refused: {response['error']}"
                )
            raw_rows = extract_rows(response.get("result"))
        except (
            _ProviderFailure, TimeoutError, ConnectionError, OSError,
        ) as exc:
            logger.warning(
                "memlock: mcp provider query failed (fail-open): %s", exc,
            )
            return []
        except Exception as exc:  # defensive: never break pre_llm_call
            logger.warning(
                "memlock: mcp provider query failed unexpectedly "
                "(fail-open): %s", exc,
            )
            return []
        finally:
            _teardown(proc)

        out: list[dict] = []
        for raw in raw_rows:
            row = normalise_row(raw)
            if row is not None:
                out.append(row)
        return out

    return get_preference_provider


get_preference_provider = make_provider()
