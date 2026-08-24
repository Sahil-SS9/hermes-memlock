"""MemLock — Re-assert standing instructions after context compaction.

Hermes Agent plugin shim.  The engine lives in memlock_core/ (harness
agnostic); this module adapts it to the Hermes plugin contract:

  - ``pre_llm_call`` hook → core compaction detection + anchor audit +
    casualty rehydration into the user turn.
  - ``guard_pin`` tool / ``/guard`` command → core pin lifecycle and status.
  - Config arrives from PluginContext or $HERMES_HOME/config.yaml; the
    Hermes compaction markers stay at their core defaults.

Detection modes: keyword probes (default) or windowed semantic similarity
(optional, needs sentence-transformers).  Injection modes: on-drift (default)
or always.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

# The engine is harness-agnostic and imports nothing Hermes-shaped. Loaded as
# a package (normal plugin install) we use relative imports; loaded as a plain
# module (plugin loaders that exec the file, pytest) the repo root is on
# sys.path and flat imports work.
try:
    from .memlock_core import MemlockService, load_config_file
    from .memlock_core import REMINDER_MARKER
except ImportError:  # plain-module load
    from memlock_core import MemlockService, load_config_file  # type: ignore[no-redef]
    from memlock_core import REMINDER_MARKER  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_CWD = Path(__file__).resolve().parent
_LOCAL_CFG = _CWD / "config.yaml"

# Back-compat re-exports (tests and third-party code import these names).
__version__ = "0.4.0"

_service = MemlockService()

# Detection helpers used to live at plugin level; keep them importable.
try:
    from .memlock_core.detection import (  # noqa: F401
        DEFAULT_SUMMARY_PREFIXES,
        audit_anchors,
        find_summary,
        hash_summary_body,
        split_context,
    )
except ImportError:  # plain-module load
    from memlock_core.detection import (  # type: ignore[no-redef]  # noqa: F401
        DEFAULT_SUMMARY_PREFIXES,
        audit_anchors,
        find_summary,
        hash_summary_body,
        split_context,
    )

# Back-compat aliases onto the service's state, so pre-extraction callers
# (and older tests) can read/patch the old module-level names. These are
# properties on the module via __getattr__ below.
_MODULE_STATE_ALIASES = {
    "_stores": "stores",
    "_session_turns": "session_turns",
}


def __getattr__(name: str):
    """Back-compat accessors for the pre-extraction module globals.

    ``_stores`` / ``_session_turns`` / ``_current_session_id`` / ``_cfg`` /
    ``_durable_store`` now live on the service instance; they are exposed
    here by reference so tests and host code that patch them keep working.
    """
    if name in _MODULE_STATE_ALIASES:
        return getattr(_service, _MODULE_STATE_ALIASES[name])
    if name == "_current_session_id":
        return _service.current_session_id
    if name == "_cfg":
        return _service._cfg
    if name == "_durable_store":
        return _service.durable_store
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# Public surface used by tests/host code. Kept as module attributes so the
# shim reads exactly like the pre-extraction plugin did.
PIN_SCHEMA_PROPERTIES = (
    "text", "unpin", "pin_id", "action", "version", "reminder", "priority",
    "probes", "scope",
)


def _service_for(session_id: str = "") -> MemlockService:
    """Return the process-wide service (single-plugin-host convenience)."""
    return _service


# ── config ──────────────────────────────────────────────────────────────


def _safe_cfg(ctx) -> dict:
    """Read plugin config from PluginContext or config.yaml fallback."""
    try:
        cfg = ctx.config.get("memlock")
        if cfg:
            return dict(cfg)
    except Exception:
        pass
    try:
        hp = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        cf = hp / "config.yaml"
        if cf.exists():
            return load_config_file(cf)
    except Exception:
        pass
    return {}


def _load_defaults() -> dict:
    """Repo-local config.yaml defaults (best-effort, fail-open)."""
    try:
        return load_config_file(_LOCAL_CFG)
    except Exception:
        return {}


def _merge_cfg(user_cfg: dict) -> dict:
    return _service.merge_cfg({**_load_defaults(), **(user_cfg or {})})


def _validate_anchors(anchors):
    """Back-compat delegate to the service method."""
    return _service.validate_anchors(anchors)


# ── back-compat delegates (pre-extraction public surface) ───────────────


def _select_casualties(store, anchored_ids, max_slots, max_chars):
    return _service.select_casualties(store, anchored_ids, max_slots, max_chars)


def _build_reminder_block(selected, remaining):
    return _service.build_reminder_block(selected, remaining)


def set_reverse_preference_provider(provider) -> None:
    """Register a host preference source (takes precedence over adapters)."""
    _service.set_preference_provider(provider)


def _reset_preference_adapter_cache() -> None:
    """Test/config-reload hook: force adapter re-resolution on next audit."""
    _service.reset_preference_adapter_cache()


def _get_store(session_id: str):
    return _service.get_store(session_id)


def _ensure_store(session_id: str):
    return _service.ensure_store(session_id)


def _get_durable():
    return _service.get_durable()


def get_service() -> MemlockService:
    """The service instance this shim drives (single-service hosts)."""
    return _service


# ── hooks ───────────────────────────────────────────────────────────────


def _on_start(session_id: str = "", **kwargs) -> None:
    _service.on_start(session_id=session_id, **kwargs)


def _on_end(session_id: str = "", **kwargs) -> None:
    _service.on_end(session_id=session_id, **kwargs)


def _on_pre_llm(
    session_id: str = "",
    turn_id: str = "",
    user_message: str = "",
    conversation_history: list | None = None,
    **kwargs,
):
    """Audit anchors post-compaction, rehydrate casualties.

    Delegates to memlock_core.MemlockService.pre_llm_turn; the returned
    context dict is appended to plugin_user_context by turn_context.py.
    """
    return _service.pre_llm_turn(
        session_id=session_id,
        turn_id=turn_id,
        user_message=user_message,
        conversation_history=conversation_history,
        **kwargs,
    )


# ── tool: guard_pin ────────────────────────────────────────────────────

_PIN_SCHEMA = {
    "name": "guard_pin",
    "description": (
        "Pin a standing instruction that must survive context compaction. "
        "Use 'text' to pin a new instruction. Use 'pin_id' with 'text' to "
        "update an existing pin in place (same anchor id, previous version "
        "kept in history). Use 'action': 'rollback' with 'pin_id' to restore "
        "a prior version from that pin's history (the displaced current "
        "version is itself kept in history; optional 'version' index selects "
        "a deeper one, default -1 = most recent previous version). Use "
        "'unpin' with an anchor id to remove a previously pinned instruction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The standing instruction to preserve (for pin or update).",
            },
            "unpin": {
                "type": "string",
                "description": "Anchor id to remove (for unpin).",
            },
            "pin_id": {
                "type": "string",
                "description": (
                    "Anchor id of an existing pin: to update in place with the new "
                    "'text', or (with action='rollback') the pin whose history to "
                    "restore from."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["rollback"],
                "description": (
                    "With pin_id: 'rollback' restores a prior version of that pin "
                    "from its history."
                ),
            },
            "version": {
                "type": "integer",
                "description": (
                    "With action='rollback': index into the pin's history. Default "
                    "-1 = most recent previous version; 0 = oldest retained."
                ),
            },
            "reminder": {
                "type": "string",
                "description": "Short version for re-insertion (optional, auto-trimmed).",
            },
            "priority": {
                "type": "integer",
                "description": "Importance 1-100, higher = re-inserted first (default 50).",
            },
            "probes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Distinctive keywords to check for survival (optional, auto-derived).",
            },
            "scope": {
                "type": "string",
                "enum": ["session", "global"],
                "description": (
                    "session = dies with session (default). global = persists across "
                    "sessions via durable store. On unpin: scope=global removes the "
                    "durable copy too; default leaves other sessions' copies intact."
                ),
            },
        },
        "required": [],
    },
}


def _pin_handler(args: dict | None = None, **kwargs) -> str:
    """Tool handler for guard_pin — delegates to the service's pin lifecycle."""
    if kwargs.get("session_id", ""):
        # Keep last-seen-session semantics for handlers without forwarding.
        _service.current_session_id = kwargs["session_id"]
    return _service.pin_handler(args, session_id=kwargs.get("session_id", ""))


# ── slash command ───────────────────────────────────────────────────────


def _status_cmd(raw_args: str = "") -> str:
    """Handle /guard slash command — integrity score and anchor status."""
    return _service.status_text(raw_args)


# ── registration ────────────────────────────────────────────────────────


def register(ctx) -> None:
    _merge_cfg(_safe_cfg(ctx))

    ctx.register_hook("on_session_start", _on_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm)
    ctx.register_hook("on_session_end", _on_end)

    # guard_pin tool — single tool with pin/unpin via args
    ctx.register_tool(
        name="guard_pin",
        toolset="guard",
        description=(
            "Pin a standing instruction that must survive context compaction. "
            "Use 'text' to pin.  Use 'pin_id' with 'text' to update an "
            "existing pin in place.  Use action='rollback' with 'pin_id' to "
            "restore a prior version from its history.  Use 'unpin' with an "
            "anchor id to remove."
        ),
        handler=_pin_handler,
        schema=_PIN_SCHEMA,
    )

    ctx.register_command(
        name="guard",
        handler=_status_cmd,
        description="MemLock status: integrity score, anchors, drift log",
    )
