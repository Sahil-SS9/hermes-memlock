"""Tool surface for the MemLock MCP server — pure dispatch, no protocol.

``build_dispatcher()`` returns ``dispatch(tool_name, arguments) -> dict``
over a MemlockService. Every tool REQUIRES ``session_id`` (caller-supplied
session identity; isolation identical to the plugin's). Results follow the
MCP tools/call result shape: ``{"content": [...], "isError": bool}`` —
errors are reported IN the result (fail-open), never raised at the caller.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:  # package import (repo root on sys.path)
    from memlock_core import MemlockService
except ImportError:  # direct execution as a script
    from mcp_server.memlock_core import MemlockService  # type: ignore[no-redef]

SERVER_NAME = "memlock"
SERVER_VERSION = "0.5.0"


# ── tool schemas (MCP tools/list entries) ────────────────────────────────

def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


SESSION_ID_PROP = {
    "session_id": {
        "type": "string",
        "description": "Caller-supplied session identity; isolates pins per session.",
    }
}

TOOLS: list[dict] = [
    _tool(
        "memlock_pin",
        "Pin a standing instruction that must survive context compaction.",
        {
            **SESSION_ID_PROP,
            "text": {"type": "string", "description": "The instruction to preserve."},
            "reminder": {"type": "string", "description": "Short version for re-insertion (optional)."},
            "priority": {"type": "integer", "description": "Importance 1-100 (default 50)."},
            "probes": {
                "type": "array", "items": {"type": "string"},
                "description": "Keywords to audit survival with (optional).",
            },
            "scope": {
                "type": "string", "enum": ["session", "global"],
                "description": "global persists across sessions via the durable store.",
            },
        },
        ["session_id"],
    ),
    _tool(
        "memlock_unpin",
        "Remove a pinned instruction by anchor id (from memlock_status).",
        {
            **SESSION_ID_PROP,
            "pin_id": {"type": "string", "description": "Anchor id to remove."},
            "scope": {
                "type": "string", "enum": ["session", "global"],
                "description": "scope=global also removes the durable copy.",
            },
        },
        ["session_id", "pin_id"],
    ),
    _tool(
        "memlock_update",
        (
            "Update an existing pin in place, keeping its id (history "
            "retained). With action='rollback' instead restores a prior "
            "version of the pin from its history (optional 'version' index; "
            "default -1 = most recent previous version)."
        ),
        {
            **SESSION_ID_PROP,
            "pin_id": {"type": "string", "description": "Anchor id of the pin to update."},
            "text": {"type": "string", "description": "The new instruction text."},
            "action": {
                "type": "string", "enum": ["rollback"],
                "description": (
                    "'rollback' restores a prior version from the pin's history "
                    "instead of updating."
                ),
            },
            "version": {
                "type": "integer",
                "description": (
                    "With action='rollback': index into the pin's history. Default "
                    "-1 = most recent previous version; 0 = oldest retained."
                ),
            },
            "reminder": {"type": "string", "description": "Short version for re-insertion (optional)."},
            "priority": {"type": "integer", "description": "Importance 1-100."},
            "probes": {"type": "array", "items": {"type": "string"}},
        },
        ["session_id", "pin_id"],
    ),
    _tool(
        "memlock_status",
        "MemLock status: integrity score, anchors, drift log for this session.",
        dict(SESSION_ID_PROP),
        ["session_id"],
    ),
    _tool(
        "memlock_audit",
        (
            "Audit pinned instructions against the conversation history; return "
            "the reminder block for the caller to inject after compaction."
        ),
        {
            **SESSION_ID_PROP,
            "conversation_history": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string"},
                        "content": {},
                    },
                },
                "description": "Messages about to be sent to the model.",
            },
            "user_message": {
                "type": "string",
                "description": "The newest user message, if not already in history.",
            },
            "summary_prefixes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "This harness's compaction-boundary literals (per call — "
                    "MCP callers pass their own; default detects none)."
                ),
            },
        },
        ["session_id"],
    ),
]

TOOL_NAMES = tuple(t["name"] for t in TOOLS)


class MemlockDispatcher:
    """Callable tool dispatcher over one MemlockService instance."""

    def __init__(self, service: "MemlockService | None" = None) -> None:
        self.service = service if service is not None else MemlockService()

    def __call__(self, tool_name: str, arguments: dict | None) -> dict:
        """Make instances directly usable as the protocol's dispatch callable."""
        return self.dispatch(tool_name, arguments)

    def list_tools(self) -> list[dict]:
        return [dict(t) for t in TOOLS]

    def dispatch(self, tool_name: str, arguments: dict | None) -> dict:
        """Execute one tool call; returns an MCP tool result dict.

        Unknown tool / missing session_id / handler failure all become
        ``isError`` results — fail-open at the protocol boundary.
        """
        args = arguments if isinstance(arguments, dict) else {}

        if tool_name == "memlock_pin":
            return self._call_pin(args)
        if tool_name == "memlock_unpin":
            return self._require(args, ("session_id", "pin_id"), self._call_unpin)
        if tool_name == "memlock_update":
            return self._require(args, ("session_id", "pin_id"), self._call_update)
        if tool_name == "memlock_status":
            return self._require(args, ("session_id",), self._call_status)
        if tool_name == "memlock_audit":
            return self._require(args, ("session_id",), self._call_audit)
        return self._error_result(f"Unknown tool: {tool_name}")

    # ── shared plumbing ──────────────────────────────────────────────────

    @staticmethod
    def _text_result(text: str, *, is_error: bool = False) -> dict:
        return {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        }

    @classmethod
    def _error_result(cls, message: str) -> dict:
        return cls._text_result(f"Error: {message}", is_error=True)

    def _require(self, args: dict, keys: tuple[str, ...], fn) -> dict:
        missing = [k for k in keys if not str(args.get(k, "") or "").strip()]
        if missing:
            return self._error_result(
                f"missing required argument(s): {', '.join(missing)}"
            )
        try:
            return fn(args)
        except Exception as exc:
            logger.warning("memlock-mcp: %s failed fail-open: %s", fn.__name__, exc)
            return self._error_result(str(exc))

    # ── tool bodies ──────────────────────────────────────────────────────

    def _call_pin(self, args: dict) -> dict:
        # session_id stays OUT of the forwarded args dict: it selects the
        # store, it is not a pin attribute.
        tool_args = {
            "text": str(args.get("text", "") or "").strip(),
            "reminder": args.get("reminder", ""),
            "priority": args.get("priority") if args.get("priority") is not None else 50,
            "probes": args.get("probes") or [],
            "scope": args.get("scope", "session"),
        }
        if not tool_args["text"]:
            return self._error_result("'text' is required to pin")
        session_id = str(args.get("session_id", "") or "").strip()
        if not session_id:
            # Fail closed HERE rather than let the core fall back to its
            # last-seen-session heuristic: over MCP there is no shared
            # session context to fall back on, and a silent wrong-store pin
            # would break isolation.
            return self._error_result("'session_id' is required to pin")
        out = self.service.pin_handler(tool_args, session_id=session_id)
        is_err = out.startswith("Error:")
        return self._text_result(out, is_error=is_err)

    def _call_unpin(self, args: dict) -> dict:
        store = self.service.ensure_store(str(args.get("session_id", "")))
        out = self.service.unpin(store, str(args.get("pin_id", "")), args)
        return self._text_result(out, is_error=out.startswith("Error:"))

    def _call_update(self, args: dict) -> dict:
        session_id = str(args.get("session_id", ""))
        store = self.service.ensure_store(session_id)
        # Rollback rides memlock_update via action='rollback': same pin_id
        # selector, no 'text' required — the restored text comes from history.
        if str(args.get("action", "")).strip().lower() == "rollback":
            out = self.service.rollback_pin(store, str(args.get("pin_id", "")), args)
            return self._text_result(out, is_error=out.startswith("Error:"))
        if not str(args.get("text", "") or "").strip():
            return self._error_result("'text' is required to update")
        priority = args.get("priority")
        out = self.service.update_pin(store, str(args.get("pin_id", "")), {
            "text": args.get("text", ""),
            "reminder": args.get("reminder", ""),
            **({"priority": priority} if priority is not None else {}),
            "probes": args.get("probes") or [],
        })
        return self._text_result(out, is_error=out.startswith("Error:"))

    def _call_status(self, args: dict) -> dict:
        out = self.service.status_text(
            "", session_id=str(args.get("session_id", "")),
        )
        return self._text_result(out)

    def _call_audit(self, args: dict) -> dict:
        session_id = str(args.get("session_id", ""))
        # MCP callers supply their harness's compaction markers per call
        # (spec: the server cannot know them). Applied for THIS call only;
        # the service's own defaults are restored before returning so one
        # caller's wording never leaks into another's.
        prefixes = args.get("summary_prefixes")
        saved = self.service.summary_prefixes
        if isinstance(prefixes, list) and prefixes:
            try:
                self.service.summary_prefixes = [str(p) for p in prefixes]
            except Exception:
                self.service.summary_prefixes = saved
        try:
            return self._audit_inner(args, session_id)
        finally:
            self.service.summary_prefixes = saved

    def _audit_inner(self, args: dict, session_id: str) -> dict:
        history = args.get("conversation_history")
        if not isinstance(history, list):
            history = []
        clean: list[dict] = []
        for msg in history:
            if isinstance(msg, dict):
                clean.append({
                    "role": str(msg.get("role", "")),
                    "content": msg.get("content", ""),
                })
        result = self.service.pre_llm_turn(
            session_id=session_id,
            user_message=str(args.get("user_message", "") or ""),
            conversation_history=clean,
        )
        block = (result or {}).get("context") if isinstance(result, dict) else None
        if not block:
            return self._text_result(
                "No rehydration needed: all pinned instructions intact."
            )
        return self._text_result(block)


def build_dispatcher(service: "MemlockService | None" = None) -> MemlockDispatcher:
    """Factory used by both the stdio loop and tests."""
    return MemlockDispatcher(service)
