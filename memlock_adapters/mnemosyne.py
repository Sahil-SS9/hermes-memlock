"""Mnemosyne adapter — read-only sqlite query against the local memory DB.

WHY stdlib-only: Mnemosyne's store is a plain SQLite file, so the adapter
needs nothing beyond Python's sqlite3 — no lazy driver dance, no network.
The connection is opened with ``mode=ro`` (immutable reader) so the audit
pass can never write to the memory provider even by accident.

DB path discovery order:
  1. ``adapter_db_path`` key in the memlock config section.
  2. ``MNEMOSYNE_DB_PATH`` / ``MNEMOSYNE_DATA_DIR`` environment variables
     (Mnemosyne's documented env conventions).
  3. ``$HERMES_HOME/mnemosyne/data/mnemosyne.db`` — the default layout
     produced by the installed plugin (config.yaml > env > defaults).

Schema note: rows are mapped from the ``working_memory`` table when
present. Column sets have drifted across Mnemosyne versions, so every
column is looked up defensively via cursor.description and missing
columns become None — matching the plugin's defensive dict.get style.

Any open/query error logs one warning and returns []: an unreachable or
locked DB must degrade, never break pre_llm_call.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_db_path(override: str | None = None) -> Path | None:
    """Config key wins; then MNEMOSYNE_* env; then HERMES_HOME default."""
    candidate = str(override or "").strip()
    if candidate:
        return Path(candidate).expanduser()
    candidate = os.environ.get("MNEMOSYNE_DB_PATH", "").strip()
    if candidate:
        return Path(candidate).expanduser()
    data_dir = os.environ.get("MNEMOSYNE_DATA_DIR", "").strip()
    if data_dir:
        return Path(data_dir).expanduser() / "mnemosyne.db"
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser(
        "~/.hermes"
    )
    return Path(home) / "mnemosyne" / "data" / "mnemosyne.db"


def _rows_as_dicts(cur: sqlite3.Cursor) -> list[dict]:
    """cursor → list of dicts keyed by column name (missing cols → absent)."""
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _map_row(raw: dict, record_type_fallback: str) -> dict:
    """Map one working_memory row onto PreferenceMemory shape.

    metadata_json is assembled from any memlock-relevant columns so the
    reverse audit can read priority/probes/standing hints without this
    adapter knowing the full metadata contract.
    """
    meta: dict[str, Any] = {}
    for key in ("priority", "probes", "standing", "material", "preference_key",
                "value"):
        if raw.get(key) is not None:
            meta[key] = raw[key]
    metadata_payload = json.dumps({"memlock": meta}) if meta else raw.get(
        "metadata_json"
    )
    timestamp = raw.get("timestamp") or raw.get("created_at")
    if isinstance(timestamp, (int, float)):
        # epoch seconds → ISO-8601 UTC, the format _parse_timestamp expects.
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
    return {
        "id": str(raw.get("id", "") or ""),
        "content": str(raw.get("content", "") or ""),
        "source": raw.get("source"),
        "timestamp": timestamp,
        "created_at": raw.get("created_at"),
        "valid_until": raw.get("valid_until"),
        "superseded_by": raw.get("superseded_by") or raw.get("superseded_by_id"),
        "scope": raw.get("scope"),
        "metadata_json": metadata_payload,
        "veracity": raw.get("veracity") or raw.get("validator"),
        "memory_type": raw.get("memory_type") or record_type_fallback,
    }


def make_provider(db_path_override: str | None = None):
    """Build the preference provider callable for Mnemosyne."""

    def get_preference_provider(query: str, limit: int) -> list[dict]:
        from memlock_adapters import normalise_row  # local: avoids cycles

        db_path = _resolve_db_path(db_path_override)
        if db_path is None or not db_path.exists():
            logger.warning(
                "memlock: mnemosyne DB not found at %s; returning no rows",
                db_path,
            )
            return []
        uri = f"file:{db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error as exc:
            logger.warning("memlock: mnemosyne connect failed (fail-open): %s", exc)
            return []
        try:
            cur = conn.execute(
                "SELECT * FROM working_memory ORDER BY rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            )
            rows = _rows_as_dicts(cur)
            cur.close()
        except sqlite3.Error as exc:
            # Missing table / locked file / corrupt page all land here.
            logger.warning("memlock: mnemosyne query failed (fail-open): %s", exc)
            return []
        finally:
            conn.close()

        out: list[dict] = []
        for raw in rows:
            row = normalise_row(_map_row(raw, "working_memory"))
            if row is not None:
                out.append(row)
        return out

    return get_preference_provider


get_preference_provider = make_provider()
