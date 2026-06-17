"""Durable pin persistence for MemLock — memory-provider agnostic.

Default backend: filesystem (JSON files in a persist directory).
Pluggable: set `backend` config key to swap in Mnemosyne or other stores.

Protocol: any backend must implement:
  save_pin(anchor_dict) -> None
  load_pins() -> list[dict]
  remove_pin(anchor_id) -> None
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class DurableStore(Protocol):
    """Protocol for durable pin backends."""

    def save_pin(self, anchor: dict) -> None: ...
    def load_pins(self) -> list[dict]: ...
    def remove_pin(self, anchor_id: str) -> None: ...


# ── FileStore (default, zero-dependency) ─────────────────────────────────


def _persist_dir() -> Path:
    """Resolved at call time so HERMES_HOME changes are honoured."""
    return Path(
        os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
        "memlock",
        "persist",
    )


class FileStore:
    """Durable pin store backed by individual JSON files.

    One file per pin: {persist_dir}/{anchor_id}.json
    No external dependencies. Works on any filesystem.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or _persist_dir()

    def _pin_path(self, anchor_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in anchor_id)
        return self._dir / f"{safe}.json"

    def save_pin(self, anchor: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._pin_path(anchor["id"])
        payload = {
            "id": anchor["id"],
            "text": anchor["text"],
            "reminder": anchor.get("reminder", anchor["text"][:120]),
            "priority": anchor.get("priority", 50),
            "probes": anchor.get("probes", []),
            "pinned": True,
            "scope": anchor.get("scope", "session"),
            "pinned_at": anchor.get("pinned_at", None),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
        tmp.rename(path)

    def load_pins(self) -> list[dict]:
        if not self._dir.exists():
            return []
        pins: list[dict] = []
        for f in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                if data.get("id") and data.get("text"):
                    pins.append(data)
            except Exception as exc:
                logger.warning("memlock: corrupt persist file %s: %s", f.name, exc)
        return pins

    def remove_pin(self, anchor_id: str) -> None:
        path = self._pin_path(anchor_id)
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("memlock: failed to remove persist file %s: %s", anchor_id, exc)


# ── factory ──────────────────────────────────────────────────────────────


def get_store(backend: str = "file", **kwargs: Any) -> DurableStore:
    """Return a DurableStore for the given backend.

    Supported backends:
      - file (default): FileStore, zero-dependency
      - mnemosyne: requires mnemosyne_remember/recall tools (deferred)

    Unknown backends fall back to FileStore with a warning.
    """
    if backend == "file":
        return FileStore(**kwargs)
    if backend == "mnemosyne":
        # Deferred — requires Hermes tool context at runtime.
        # Placeholder: falls back to FileStore until wired.
        logger.warning(
            "memlock: mnemosyne persistence backend not yet implemented; "
            "falling back to file store"
        )
        return FileStore(**kwargs)
    logger.warning(
        "memlock: unknown persistence backend '%s'; falling back to file store",
        backend,
    )
    return FileStore(**kwargs)
