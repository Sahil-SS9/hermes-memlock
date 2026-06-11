"""Per-session atomic JSON store for MemLock.

Each session gets one file under ~/.hermes/memlock/<safe-sid>.json
with anchors (static + pinned), integrity score, drift log, compaction state,
and alert timestamp.  Writes use tmp + rename for atomicity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_DIR = Path(
    os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
    "memlock",
)


def _safe_sid(session_id: str) -> str:
    """Slugify a session id into a safe filename component."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)


def _store_path(session_id: str) -> Path:
    return _STORE_DIR / f"{_safe_sid(session_id)}.json"


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)


def _blank() -> dict:
    return {
        "session_id": "",
        "anchors": {},
        "static_anchor_ids": [],
        "pinned_count": 0,
        "last_summary_hash": None,
        "last_compaction_at": None,
        "integrity_score": None,
        "drift_log": [],
        "last_alert_at": None,
        "last_reinject_turn": 0,
    }


class SessionStore:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._path = _store_path(session_id)
        self._data: dict[str, Any] = self._load()

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text())
            # overlay any keys missing in earlier schema versions
            blank = _blank()
            blank.update(raw)
            blank["session_id"] = self.session_id
            return blank
        except Exception:
            return {
                **_blank(),
                "session_id": self.session_id,
            }

    def save(self) -> None:
        try:
            _atomic_write(self._path, self._data)
        except Exception as exc:
            logger.warning("memlock: store write failed: %s", exc)

    # ── anchors ────────────────────────────────────────────────────────

    def add_anchor(
        self,
        anchor_id: str,
        text: str,
        reminder: str,
        priority: int,
        probes: list[str],
        pinned: bool = True,
    ) -> None:
        self._data["anchors"][anchor_id] = {
            "id": anchor_id,
            "text": text,
            "reminder": reminder,
            "priority": priority,
            "probes": probes,
            "pinned": pinned,
            "drifted": False,
            "last_alive_turn": 0,
        }
        if pinned:
            self._data["pinned_count"] = sum(
                1 for a in self._data["anchors"].values() if a["pinned"]
            )
        else:
            self._data.setdefault("static_anchor_ids", [])
            if anchor_id not in self._data["static_anchor_ids"]:
                self._data["static_anchor_ids"].append(anchor_id)
        self.save()

    def unpin(self, anchor_id: str) -> bool:
        a = self._data["anchors"].get(anchor_id)
        if a is None or not a["pinned"]:
            return False
        del self._data["anchors"][anchor_id]
        self._data["pinned_count"] = sum(
            1 for a in self._data["anchors"].values() if a["pinned"]
        )
        self.save()
        return True

    def anchors(self) -> dict[str, dict]:
        return dict(self._data["anchors"])

    def sorted_anchors(self) -> list[dict]:
        return sorted(
            self._data["anchors"].values(),
            key=lambda a: (-a["priority"], a["id"]),
        )

    # ── compaction detection ───────────────────────────────────────────

    def is_new_compaction(self, summary_hash: str | None) -> bool:
        """Return True if summary_hash is new or changed since last compaction."""
        if summary_hash is None:
            return False
        return summary_hash != self._data.get("last_summary_hash")

    def record_compaction(self, summary_hash: str | None, turn: int) -> None:
        self._data["last_summary_hash"] = summary_hash
        self._data["last_compaction_at"] = time.time()
        self.save()

    @property
    def last_summary_hash(self) -> str | None:
        return self._data.get("last_summary_hash")

    # ── drift / score ──────────────────────────────────────────────────

    def mark_anchor_alive(self, anchor_id: str, turn: int) -> None:
        a = self._data["anchors"].get(anchor_id)
        if a:
            a["drifted"] = False
            a["last_alive_turn"] = turn

    def mark_anchor_drifted(self, anchor_id: str) -> None:
        a = self._data["anchors"].get(anchor_id)
        if a:
            a["drifted"] = True

    def compute_integrity_score(self) -> int:
        total = len(self._data["anchors"])
        if total == 0:
            self._data["integrity_score"] = None
            return -1  # sentinel: no anchors
        survived = sum(
            1 for a in self._data["anchors"].values() if not a["drifted"]
        )
        score = round(100 * survived / total)
        self._data["integrity_score"] = score
        return score

    def log_drift(self, casualties: list[str], score: int) -> None:
        self._data["drift_log"].append({
            "time": time.time(),
            "score": score,
            "casualties": casualties,
        })
        # keep last 20 drift events
        self._data["drift_log"] = self._data["drift_log"][-20:]

    @property
    def integrity_score(self) -> int | None:
        return self._data.get("integrity_score")

    # ── alert cooldown ─────────────────────────────────────────────────

    def can_alert(self, cooldown_s: float) -> bool:
        last = self._data.get("last_alert_at")
        if last is None:
            return True
        return (time.time() - last) >= cooldown_s

    def record_alert(self) -> None:
        self._data["last_alert_at"] = time.time()
        self.save()

    # ── reinjection turn ───────────────────────────────────────────────

    @property
    def last_reinject_turn(self) -> int:
        return self._data.get("last_reinject_turn", 0)

    def set_reinject_turn(self, turn: int) -> None:
        self._data["last_reinject_turn"] = turn
        self.save()
