"""Severian adapter — read-only preference query against PostgreSQL.

WHY read-only and why fail-open: the reverse audit is a diagnostic pass
inside pre_llm_call. It must never mutate the canonical memory store
(lifecycle changes are a separate reviewed workflow in Severian itself)
and must never take down a Hermes turn because the database was
unreachable. Every expected failure mode degrades to an empty result.

DSN discovery order:
  1. ``adapter_dsn`` key in the memlock config section (passed by the
     plugin when building the adapter).
  2. ``SEVERIAN_DSN`` environment variable — the same var Severian's own
     Hermes integration reads (src/severian/integrations/hermes.py),
     so a working Severian install needs zero extra configuration.

Schema note: Severian's canonical table (migrations/versions/0001) is
``records`` with columns id, record_type, tenant_id, ..., status,
payload(JSONB). Observation/Memory payloads carry content, created_at,
source and superseded_by_id inside that JSONB. The adapter selects only
record types that look like preferences/memories, maps payload keys onto
the detection.PreferenceMemory shape, and treats missing payload keys as
absent — never as errors.

The psycopg2/psycopg driver is imported lazily at query time; ImportError
is one warning plus an empty list, matching every other fail-open path.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Only rows whose record_type maps to derived knowledge are candidates;
# observations are raw evidence and jobs are queue internals.
_RECORD_TYPES = ("memory", "observation", "preference")

_QUERY = (
    "SELECT id, record_type, status, payload "
    "FROM records WHERE record_type = ANY(%s) ORDER BY id LIMIT %s"
)


def _resolve_dsn(override: str | None) -> str:
    """adapter_dsn config wins; SEVERIAN_DSN env is the documented default."""
    dsn = str(override or "").strip()
    if dsn:
        return dsn
    return os.environ.get("SEVERIAN_DSN", "").strip()


def _load_payload(payload: Any) -> dict:
    """payload may arrive already-decoded (psycopg2 json→dict) or as text."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (str, bytes, bytearray)):
        try:
            decoded = json.loads(payload)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def make_provider(dsn_override: str | None = None):
    """Build the preference provider callable for Severian."""

    def get_preference_provider(query: str, limit: int) -> list[dict]:
        from memlock_adapters import normalise_row  # local: avoids cycles

        dsn = _resolve_dsn(dsn_override)
        if not dsn:
            logger.warning(
                "memlock: severian adapter has no DSN "
                "(set memlock.adapter_dsn or SEVERIAN_DSN); returning no rows"
            )
            return []
        try:
            # Lazy driver import: psycopg2 is NOT a plugin dependency.
            try:
                import psycopg2  # type: ignore[import-untyped]
                connect = psycopg2.connect
            except ImportError:
                import psycopg  # type: ignore[import-not-found]
                connect = psycopg.connect
        except ImportError as exc:
            logger.warning(
                "memlock: psycopg not installed for severian adapter: %s", exc
            )
            return []

        try:
            # connect_timeout keeps an unreachable-but-not-refusing PG from
            # hanging the pre_llm_call hook past the fail-open contract (L2).
            with connect(dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(_QUERY, (list(_RECORD_TYPES), max(1, int(limit))))
                    rows = cur.fetchall() or []
        except Exception as exc:
            logger.warning(
                "memlock: severian query failed (fail-open): %s", exc,
            )
            return []

        out: list[dict] = []
        for rid, record_type, _status, payload in rows:
            data = _load_payload(payload)
            row = normalise_row({
                "id": str(rid or ""),
                "content": data.get("content"),
                "source": (data.get("source") or {}).get("kind")
                if isinstance(data.get("source"), dict)
                else data.get("source"),
                "timestamp": data.get("created_at"),
                "created_at": data.get("created_at"),
                "valid_until": data.get("valid_until"),
                "superseded_by": data.get("superseded_by_id"),
                "scope": data.get("scope") if isinstance(data.get("scope"), str) else None,
                "metadata_json": json.dumps(data)
                if data and data.get("memlock")
                else None,
                "veracity": data.get("veracity"),
                "memory_type": record_type,
            })
            if row is not None:
                out.append(row)
        return out

    return get_preference_provider


get_preference_provider = make_provider()
