"""Durable pin persistence for MemLock — memory-provider agnostic.

Default backend: filesystem (JSON files in a persist directory).
Pluggable: set `backend` config key to swap in Mnemosyne or other stores.

Protocol: any backend must implement:
  save_pin(anchor_dict) -> None
  load_pins() -> list[dict]
  remove_pin(anchor_id) -> None
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class BackendUnavailableError(RuntimeError):
    """Raised when a configured backend has no documented host adapter."""


class DurableStore(Protocol):
    """Protocol for durable pin backends."""

    def save_pin(self, anchor: dict) -> None: ...
    def load_pins(self) -> list[dict]: ...
    def remove_pin(self, anchor_id: str) -> None: ...


# ── FileStore (default, zero-dependency) ─────────────────────────────────


def _persist_dir() -> Path:
    """Resolved at call time so host-home env changes are honoured.

    Env contract mirrors memlock_core.store: HERMES_HOME (host-provided) or
    MEMLOCK_HOME, else the harness-neutral default under the user's home.
    """
    for var in ("HERMES_HOME", "MEMLOCK_HOME"):
        val = os.environ.get(var, "").strip()
        if val:
            return Path(val, "memlock", "persist")
    return Path(os.path.join(os.path.expanduser("~"), ".memlock"), "memlock", "persist")


MANIFEST_NAME = "manifest.json"
#: Bumped when the manifest schema changes; load treats unknown versions as
#: unreadable and bootstraps a fresh baseline (fail-open).
MANIFEST_VERSION = 1


def sha256_file(path: Path) -> str:
    """sha256 hex digest of a file's bytes; missing files hash as absent."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError as exc:
        logger.warning("memlock: could not read %s for hashing: %s", path.name, exc)
        raise
    return h.hexdigest()


class FileStore:
    """Durable pin store backed by individual JSON files.

    One file per pin: {persist_dir}/{anchor_id}.json
    No external dependencies. Works on any filesystem.
    """

    def __init__(self, directory: Path | None = None, *, verify: bool = True) -> None:
        self._dir = directory or _persist_dir()
        # verify=False opts out of integrity checking (setup wizard reads,
        # prune paths); the default is enforcing.
        self.verify = verify
        # Stems that failed an integrity check on load. They are excluded
        # from subsequent manifest writes so tampered bytes are never
        # re-baselined (M3). In-memory per store instance: a fresh process
        # re-derives quarantine from the manifest on first load anyway.
        self._quarantined_stems: set[str] = set()

    def _pin_path(self, anchor_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in anchor_id)
        return self._dir / f"{safe}.json"

    def _manifest_path(self) -> Path:
        return self._dir / MANIFEST_NAME

    def _entry_for(self, path: Path) -> dict | None:
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError as exc:
            # Fail-open: an unreadable file gets omitted from manifest rather than
            # blocking the save; load will re-baseline it if needed.
            logger.warning("memlock: could not stat/hash %s: %s", path.name, exc)
            return None
        return {"sha256": digest, "size": size}

    def _write_manifest(self) -> None:
        """Write manifest.json atomically over every current pin file.

        Two protections shape this write (M1/M2/M3):
        - read-modify-MERGE: entries for files we are not touching survive,
          so a concurrent writer's pins can't be erased by our snapshot;
        - files that failed integrity checks are excluded entirely, so
          quarantined bytes are never silently re-baselined.
        """
        # Start from the on-disk manifest so unrelated entries survive (M1),
        # then reconcile with reality on disk:
        # - files present + readable -> fresh hash entry
        # - files gone -> entry dropped (removal is explicit intent)
        # - quarantined stems -> keep OLD entry so tampered bytes stay
        #   rejected on every future load, never re-baselined (M3)
        # - unreadable now -> stale entry dropped rather than zero-hashed (M2)
        existing = self._load_manifest_pins() or {}
        pins: dict[str, dict] = {}
        seen_stems: set[str] = set()
        for f in sorted(self._dir.glob("*.json")):
            if f.name == MANIFEST_NAME:
                continue
            seen_stems.add(f.stem)
            if f.stem in self._quarantined_stems:
                # Tampered bytes: keep the OLD entry so the file stays
                # quarantined; never record a hash of tampered content.
                logger.warning(
                    "memlock: manifest write skips quarantined pin %s", f.name,
                )
                if f.stem in existing:
                    pins[f.stem] = existing[f.stem]
                continue
            entry = self._entry_for(f)
            if entry is not None:
                pins[f.stem] = entry
            else:
                # Unreadable now: drop any stale entry rather than recording
                # a zero-hash that would permanently mismatch later (M2).
                logger.warning(
                    "memlock: manifest write omits unreadable pin %s", f.name,
                )
        manifest = {
            "version": MANIFEST_VERSION,
            "generated_at": time.time(),
            "pins": pins,
        }
        mpath = self._manifest_path()
        tmp = mpath.with_suffix(mpath.suffix + ".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        tmp.rename(mpath)

    def save_pin(self, anchor: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        # L4: 'manifest' is a reserved stem — a pin with that id would write
        # over manifest.json itself and bootstrap a no-verification baseline.
        anchor = dict(anchor)
        if str(anchor.get("id", "")).strip().lower() == MANIFEST_NAME.removesuffix(".json"):
            logger.warning(
                "memlock: pin id 'manifest' is reserved; renaming to 'manifest_pin'"
            )
            anchor["id"] = "manifest_pin"
            if isinstance(anchor.get("text"), str) and not anchor.get("reminder"):
                anchor["reminder"] = anchor["text"][:120]
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
        self._write_manifest()

    def _load_manifest_pins(self) -> dict[str, dict] | None:
        """Read manifest.json; None means no usable baseline (bootstrap)."""
        try:
            raw = json.loads(self._manifest_path().read_text())
        except FileNotFoundError:
            return None  # bootstrap: fresh baseline from current files
        except Exception as exc:
            logger.warning(
                "memlock: unreadable pin manifest (%s); rebuilding baseline", exc,
            )
            return None
        pins = raw.get("pins")
        if not isinstance(pins, dict):
            logger.warning("memlock: pin manifest has no pins table; rebuilding")
            return None
        return {str(k): v for k, v in pins.items() if isinstance(v, dict)}

    def load_pins(self) -> list[dict]:
        """Load durable pins, verifying each file against the manifest.

        Integrity is ENFORCING for durable pins: a file whose bytes do not
        match the recorded sha256 is quarantined (skipped + logged), never
        silently loaded. A missing or unreadable manifest bootstraps a fresh
        baseline from the files on disk instead of failing.
        """
        if not self._dir.exists():
            return []
        baseline = (
            self._load_manifest_pins() if self.verify else None
        )
        quarantined: list[str] = []
        pins: list[dict] = []
        for f in sorted(self._dir.glob("*.json")):
            if f.name == MANIFEST_NAME:
                continue
            if baseline is not None and f.stem in baseline:
                expected = baseline[f.stem].get("sha256", "")
                try:
                    actual = sha256_file(f)
                except OSError:
                    quarantined.append(f.name)
                    continue
                if actual != expected:
                    logger.warning(
                        "memlock: quarantine — pin file %s failed integrity "
                        "check (hash mismatch vs manifest)", f.name,
                    )
                    quarantined.append(f.name)
                    continue
            try:
                data = json.loads(f.read_text())
                if data.get("id") and data.get("text"):
                    pins.append(data)
                else:
                    logger.warning(
                        "memlock: persist file %s missing id/text; skipped", f.name,
                    )
            except Exception as exc:
                logger.warning("memlock: corrupt persist file %s: %s", f.name, exc)
        if quarantined:
            logger.warning(
                "memlock: %d pin file(s) quarantined by integrity check: %s",
                len(quarantined), ", ".join(quarantined),
            )
            # Remember them so _write_manifest never re-baselines tampered
            # bytes (M3).
            for name in quarantined:
                self._quarantined_stems.add(name[:-len(".json")] if name.endswith(".json") else name)
        return pins

    def remove_pin(self, anchor_id: str) -> None:
        path = self._pin_path(anchor_id)
        removed = False
        try:
            path.unlink(missing_ok=True)
            removed = not path.exists()
        except Exception as exc:
            logger.warning(
                "memlock: failed to remove persist file %s: %s", anchor_id, exc,
            )
        if removed:
            try:
                self._write_manifest()
            except Exception as exc:
                logger.warning("memlock: manifest rewrite after remove failed: %s", exc)


# ── factory ──────────────────────────────────────────────────────────────


def get_store(backend: str = "file", **kwargs: Any) -> DurableStore:
    """Return a DurableStore for the given backend.

    Supported backends:
      - file (default): FileStore, zero-dependency
      - mnemosyne: unavailable until the host supplies a documented adapter

    Unknown backends fall back to FileStore with a warning.
    """
    if backend == "file":
        return FileStore(**kwargs)
    if backend == "mnemosyne":
        raise BackendUnavailableError(
            "mnemosyne persistence requires a documented host adapter; "
            "filesystem fallback would misrepresent the selected backend"
        )
    logger.warning(
        "memlock: unknown persistence backend '%s'; falling back to file store",
        backend,
    )
    return FileStore(**kwargs)
