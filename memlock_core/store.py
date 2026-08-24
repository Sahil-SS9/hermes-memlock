"""Per-session atomic JSON store for MemLock.

Each session gets one file under $MEMLOCK_HOME/memlock/<safe-sid>.json
(MEMLOCK_HOME falls back to the host home directory) with anchors
(static + pinned), integrity score, drift log, compaction state, and alert
timestamp.  Writes use tmp + rename for atomicity.
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

#: Host-home environment variable. Hosts may point MemLock's storage at a
#: custom root by setting this (or MEMLOCK_HOME) before first use; the
#: default keeps the historical layout under the user's home directory.
_HOME_ENV_VARS = ("HERMES_HOME", "MEMLOCK_HOME")


def _home_dir() -> str:
    """First set host-home env var, else the user's home directory.

    The fallback is deliberately harness-neutral: MEMLOCK_HOME under the
    user's plain home.  Harnesses that own a home directory (Hermes, etc.)
    pass it in via their env-var contract, so core never names one.
    """
    for var in _HOME_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return os.path.join(os.path.expanduser("~"), ".memlock")


def _store_dir() -> Path:
    # Resolved at call time so env vars set after import (tests, embedding
    # hosts) are honoured.
    return Path(_home_dir(), "memlock")


def _safe_sid(session_id: str) -> str:
        """Slugify a session id into a safe filename component.

        We want different session ids to map to different filenames where possible.
        Replace '/' with '_slash_' and '.' with '_dot_' to avoid collisions
        like 'sess/a' vs 'sess.a'. All other non-alnum/not-in-'-_' become '_'.
        """
        mapping = {'/': '_slash_', '.': '_dot_'}
        res = []
        for c in session_id:
            if c.isalnum() or c in '-_':
                res.append(c)
            elif c in mapping:
                res.append(mapping[c])
            else:
                res.append('_')
        return ''.join(res)


def _store_path(session_id: str) -> Path:
    return _store_dir() / f"{_safe_sid(session_id)}.json"


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
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
        "turn_count": 0,
    }


class SessionStore:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._data: dict[str, Any] = self._load()
        # non-persisted turn count for safety net
        self._turn_count: int = 0

    @property
    def _path(self) -> Path:
        # Recomputed per access so a HERMES_HOME change mid-process is
        # honoured by long-lived instances, not just new ones.
        return _store_path(self.session_id)

    # ── persistence ────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text())
            # Integrity is ADVISORY for session stores: a hash mismatch is a
            # warning + proceed (the file may be mid-write from another
            # process), unlike durable pins where mismatches quarantine.
            stored_hash = raw.pop("self_sha256", None)
            anchors_payload = raw.get("anchors")
            if stored_hash:
                actual = hashlib.sha256(
                    json.dumps(
                        anchors_payload, sort_keys=True, ensure_ascii=False,
                    ).encode("utf-8"),
                ).hexdigest()
                if actual != stored_hash:
                    logger.warning(
                        "memlock: session store %s failed self-integrity "
                        "check (anchors sha256 mismatch); loading anyway",
                        self._path.name,
                    )
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
            data = dict(self._data)
            data["self_sha256"] = hashlib.sha256(
                json.dumps(
                    self._data.get("anchors"), sort_keys=True,
                    ensure_ascii=False,
                ).encode("utf-8"),
            ).hexdigest()
            _atomic_write(self._path, data)
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

    def get_anchor(self, anchor_id: str) -> dict | None:
        return self._data["anchors"].get(anchor_id)

    # Cap on retained previous versions per anchor (minimal versioning).
    HISTORY_CAP = 5

    def update_anchor(
        self,
        anchor_id: str,
        *,
        text: str,
        reminder: str,
        priority: int,
        probes: list[str],
    ) -> bool:
        """Update text/reminder/priority/probes IN PLACE, keeping the id.

        The pre-update version dict is appended to an ``history`` list on the
        anchor so /guard-style introspection can show what changed; only the
        last HISTORY_CAP versions are retained. Returns False if the anchor
        does not exist (callers decide whether that is an error).
        """
        a = self._data["anchors"].get(anchor_id)
        if a is None:
            return False
        history = a.setdefault("history", [])
        history.append({
            "text": a["text"],
            "reminder": a["reminder"],
            "priority": a["priority"],
            "probes": list(a["probes"]),
            "updated_at": time.time(),
        })
        a["history"] = history[-self.HISTORY_CAP:]
        a["text"] = text
        a["reminder"] = reminder
        a["priority"] = priority
        a["probes"] = probes
        self.save()
        return True

    def anchors(self) -> dict[str, dict]:
        return dict(self._data["anchors"])

    def rollback_anchor(self, anchor_id: str, *, version: int = -1) -> bool:
        """Restore a prior version of an anchor from its history.

        Selection semantics: ``version`` indexes into the anchor's history
        list. The default (-1) is the MOST RECENT previous version; any
        other negative or positive index follows Python list indexing
        (0 = oldest retained version). Out-of-range selections return False.

        Restoring pushes the DISPLACED current state onto the front of the
        history (nothing is destroyed), then the HISTORY_CAP trim applies.
        Returns False if the anchor does not exist or has no history.
        """
        a = self._data["anchors"].get(anchor_id)
        if a is None:
            return False
        history = a.get("history") or []
        try:
            victim = history[version]
        except IndexError:
            return False
        if not isinstance(victim, dict):
            return False
        # Displaced CURRENT state becomes a history entry first.
        history.append({
            "text": a["text"],
            "reminder": a["reminder"],
            "priority": a["priority"],
            "probes": list(a["probes"]),
            "updated_at": time.time(),
            "rolled_back_from": version,
        })
        restored = {
            "text": str(victim.get("text", "")),
            "reminder": str(victim.get("reminder", victim.get("text", "")[:120])),
            "priority": int(victim.get("priority", 50)),
            "probes": [str(p) for p in (victim.get("probes") or [])],
        }
        # Drop the consumed version, keep the displaced one — net count only
        # grows by one, so the cap trims exactly as an update would.
        history.remove(victim)
        a["history"] = history[-self.HISTORY_CAP:]
        a.update(restored)
        self.save()
        return True

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

    def log_drift(
        self, casualties: list[str], score: int, *, reverse: dict | None = None,
    ) -> None:
        entry = {
            "time": time.time(),
            "score": score,
            "casualties": casualties,
        }
        if reverse is not None:
            entry["reverse"] = reverse
        self._data["drift_log"].append(entry)
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

    @property
    def turn_count(self) -> int:
        return self._data.get("turn_count", 0)

    def increment_turn(self) -> int:
        current = self._data.get("turn_count", 0) + 1
        self._data["turn_count"] = current
        self.save()
        return current
