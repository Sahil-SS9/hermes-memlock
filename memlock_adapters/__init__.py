"""Adapter layer for MemLock's reverse-audit preference providers.

WHY this package exists: MemLock is memory-provider agnostic. The reverse
audit needs *some* source of stored user preferences, but the core plugin
must never import a named provider. This package is the only place that
knows about specific providers, and each adapter module is imported
lazily, at query time, so a missing provider dependency (psycopg2, etc.)
can never break the pre_llm_call hook — every adapter fails open to an
empty list.

Contract (mirrors persistence.py's documented protocol style):

  get_provider(name: str) -> PreferenceProvider | None

  A PreferenceProvider is any callable matching:

      get_preference_provider(query: str, limit: int) -> list[dict]

  and returning rows shaped like detection.PreferenceMemory:
    id, content, source, timestamp, created_at, valid_until,
    superseded_by, scope, metadata_json, veracity, memory_type

  Adapters NEVER raise for expected failure modes (missing driver,
  unreachable DB, malformed rows): they log one warning and return [].
  Rows that cannot be mapped are skipped rather than aborting the batch.
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# A preference provider: query + limit in, complete preference rows out.
PreferenceProvider = Callable[[str, int], list[dict]]

# Registry of adapter names -> zero-arg factory returning a provider.
# Kept as strings->callables so importing this package never imports a
# provider dependency.
_ADAPTER_FACTORIES: dict[str, Callable[[], PreferenceProvider]] = {}


def register_adapter(name: str, factory: Callable[[], PreferenceProvider]) -> None:
    """Register an adapter factory under ``name`` (used by tests too)."""
    _ADAPTER_FACTORIES[name] = factory


def available_adapters() -> list[str]:
    """Names of all known adapters."""
    return sorted(_ADAPTER_FACTORIES)


def get_provider(name: str) -> PreferenceProvider | None:
    """Return the config-selected preference provider for ``name``.

    Unknown names return None with a warning — callers treat None as
    "reverse audit disabled" (fail-open), never as an error.
    """
    factory = _ADAPTER_FACTORIES.get(str(name).strip().lower())
    if factory is None:
        logger.warning(
            "memlock: unknown preference adapter '%s'; reverse audit disabled",
            name,
        )
        return None
    try:
        provider = factory()
    except Exception as exc:
        # Factory construction itself must not take down the hook.
        logger.warning(
            "memlock: adapter '%s' failed to initialise: %s; disabled", name, exc,
        )
        return None
    return provider


# Built-in adapters are imported lazily inside their factories so merely
# importing memlock_adapters pulls in stdlib only.
register_adapter(
    "severian",
    lambda: __import__(
        "memlock_adapters.severian", fromlist=["get_preference_provider"]
    ).get_preference_provider,
)
register_adapter(
    "mnemosyne",
    lambda: __import__(
        "memlock_adapters.mnemosyne", fromlist=["get_preference_provider"]
    ).get_preference_provider,
)
# Generic MCP-first adapter: any external memory provider speaking MCP
# (stdio) works with zero bespoke code. Config keys mcp_command / mcp_url /
# mcp_tool / mcp_timeout_s; stdlib-only, so the lazy-import dance is only
# about keeping this module's import cost out of non-MCP installs.
register_adapter(
    "mcp",
    lambda: __import__(
        "memlock_adapters.mcp_provider", fromlist=["get_preference_provider"]
    ).get_preference_provider,
)


def normalise_row(raw: object) -> dict | None:
    """Coerce a provider row into detection.PreferenceMemory shape.

    Returns None for rows without a usable id AND content (the reverse
    audit classifies those as UNKNOWN/review anyway, but dropping them
    here keeps the caller's row/finding join deterministic).

    Missing optional columns become None so downstream dict.get chains
    never see KeyError.
    """
    if not isinstance(raw, dict):
        return None
    rid = str(raw.get("id", "") or "").strip()
    content = str(raw.get("content", "") or "").strip()
    if not rid or not content:
        return None
    row: dict = {
        "id": rid,
        "content": content,
        "source": raw.get("source"),
        "timestamp": _iso_or_none(raw.get("timestamp")),
        "created_at": _iso_or_none(raw.get("created_at")),
        "valid_until": _iso_or_none(raw.get("valid_until")),
        "superseded_by": raw.get("superseded_by") or None,
        "scope": raw.get("scope"),
        "metadata_json": raw.get("metadata_json"),
        "veracity": raw.get("veracity"),
        "memory_type": raw.get("memory_type"),
    }
    return row


def _iso_or_none(value: object) -> str | None:
    """Best-effort ISO string coercion; datetimes become ISO-8601."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return str(iso())
        except Exception:
            return None
    return None
