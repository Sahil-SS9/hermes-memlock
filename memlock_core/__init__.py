"""MemLock core — harness-agnostic context-compaction protection.

WHY this package exists: MemLock's detection, auditing, pin lifecycle and
reminder building are useful under any agent harness, but the original
implementation was fused to a single host's plugin contract. This package is
the extracted engine:

  - ZERO harness imports. Compaction markers (``summary_prefixes``),
    reminder marker wording and store locations are constructor/config
    parameters with neutral defaults; no harness name appears anywhere in
    this code (enforced by test).
  - Memory-provider agnostic: no named provider is imported here.
    Preference sources arrive as plain callables (see
    set_preference_provider) chosen by the host or its adapter layer.
  - Fail-open everywhere: a broken preference provider, corrupt store file
    or missing config degrades to a warning + safe default, never an
    exception escaping to the harness.

The :class:`MemlockService` bundles the mutable state the old plugin module
kept as globals (per-session stores, turn counters, config), so each
harness shim owns one instance instead of sharing process-wide globals.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from .detection import (
    DEFAULT_SUMMARY_PREFIXES,
    _derive_probes,
    audit_anchors,
    find_summary,
    hash_summary_body,
    reverse_audit,
    reverse_audit_unavailable,
    semantic_audit_anchors,
    split_context,
)
from .persistence import get_store as _get_durable_store
from .store import SessionStore

logger = logging.getLogger(__name__)

# Stable marker for reminder blocks.  Phrasing rules:
#  - declarative, innocuous, no urgency theatre
#  - no self-concealing language
#  - stable wording for cache-friendliness
REMINDER_MARKER = "[Standing instructions — still active]"

PreferenceProvider = Callable[[str, int], list[dict]]

__version__ = "0.4.0"


class MemlockService:
    """Harness-agnostic MemLock engine: one instance per harness shim."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        summary_prefixes: list[str] | None = None,
        reminder_marker: str | None = None,
    ) -> None:
        # summary_prefixes: compaction boundary markers for THIS harness.
        # None keeps today's neutral defaults (the frozen literals in
        # memlock_core.detection), which match the original host's
        # compressor so existing installs detect compactions unchanged.
        self.summary_prefixes = (
            list(summary_prefixes)
            if summary_prefixes is not None
            else list(DEFAULT_SUMMARY_PREFIXES)
        )
        self.reminder_marker = (
            reminder_marker if reminder_marker is not None else REMINDER_MARKER
        )
        self._cfg: dict[str, Any] = dict(config or {})
        self.stores: dict[str, SessionStore] = {}
        self.session_turns: dict[str, int] = {}
        self.current_session_id: str = ""
        self.durable_store: Any = None  # lazy-init on first use

        # Explicitly-registered preference source wins over cfg-selected
        # adapters for the lifetime of this service.
        self._explicit_preference_provider: PreferenceProvider | None = None
        # _UNSET distinguishes "adapter not resolved yet" from "resolved and
        # disabled (None)" so lazy resolution runs exactly once per config
        # generation.
        self._UNSET = object()
        self._preference_adapter: Any = self._UNSET

    # ── config ──────────────────────────────────────────────────────────

    def merge_cfg(self, user_cfg: dict) -> dict:
        """Overlay user config on top of the constructor config."""
        merged = dict(self._cfg)
        merged.update(user_cfg or {})
        self._cfg = merged
        return self._cfg

    def validate_anchors(self, anchors: list[dict]) -> list[dict]:
        """Validate static anchors from config.  Drops any that fail validation.

        Probe-less anchors cannot be audited individually, so they are only
        accepted when they are the sole anchor defined.  The rule is applied
        against the total config count, not insertion order, so the same set
        is accepted or rejected identically regardless of ordering.
        """
        total = sum(1 for a in anchors if a.get("id") and a.get("text"))
        valid: list[dict] = []
        for a in anchors:
            aid = a.get("id", "")
            text = a.get("text", "")
            reminder = a.get("reminder", text[:120])
            probes = a.get("probes", [])
            if not aid or not text:
                logger.warning("memlock: skipping anchor missing id or text")
                continue
            if len(probes) < 1 and total > 1:
                logger.warning(
                    "memlock: anchor '%s' has 0 probes and %d anchors are "
                    "defined; rejecting to prevent ambiguous audits", aid, total,
                )
                continue
            if len(probes) < 1:
                logger.warning(
                    "memlock: anchor '%s' has no probes; audits treat it as "
                    "always alive", aid,
                )
            valid.append({
                "id": aid,
                "text": text,
                "reminder": reminder or text[:120],
                "priority": int(a.get("priority", 50)),
                "probes": [str(p) for p in probes],
                "pinned": bool(a.get("pinned", False)),
            })
        return valid

    # ── rehydration selection ───────────────────────────────────────────

    def select_casualties(
        self,
        store: SessionStore,
        anchored_ids: list[str],
        max_slots: int,
        max_chars: int,
    ) -> tuple[list[dict], list[str]]:
        """Select drifted anchors for rehydration, packed by priority→id alpha.

        Returns (selected_anchors, remaining_casualty_ids).
        """
        all_anchors = store.sorted_anchors()
        casualties = [a for a in all_anchors if a["id"] in anchored_ids]
        selected: list[dict] = []
        total_chars = 0

        for anchor in casualties:
            if len(selected) >= max_slots:
                break
            reminder_text = anchor.get("reminder", anchor.get("text", "")[:120])
            new_chars = len(reminder_text) + 4  # "  - \n"
            if total_chars + new_chars > max_chars:
                continue
            selected.append(anchor)
            total_chars += new_chars

        remaining = [a["id"] for a in casualties if a not in selected]
        return selected, remaining

    def build_reminder_block(
        self,
        selected: list[dict],
        remaining: list[str],
    ) -> str | None:
        """Build the reminder injection string.  None if nothing to inject."""
        if not selected:
            return None
        lines = [self.reminder_marker]
        for a in selected:
            reminder = a.get("reminder", a.get("text", ""))[:120]
            lines.append(f"  - {reminder}")
        if remaining:
            lines.append(
                f"  - ({len(remaining)} additional instruction(s) not shown — "
                f"the user can run /guard to see the full list)"
            )
        return "\n".join(lines)

    # ── preference provider resolution ──────────────────────────────────

    def set_preference_provider(self, provider: PreferenceProvider | None) -> None:
        """Register an explicit preference source (host adapter fast path).

        Explicitly-registered providers take precedence over config-selected
        adapters (``preference_adapter``) for the lifetime of the service.
        """
        self._explicit_preference_provider = provider

    def _resolve_preference_provider(self) -> PreferenceProvider | None:
        """Pick the preference provider: explicit registration > config adapter.

        The config-selected adapter is resolved through the optional
        ``adapter_resolver`` callable (wired by the host shim to its adapter
        package). The core never imports an adapter package itself: unknown
        names / missing resolvers disable the reverse pass with a warning —
        fail-open, never an error.
        """
        if self._explicit_preference_provider is not None:
            return self._explicit_preference_provider
        if self._preference_adapter is not self._UNSET:
            return self._preference_adapter

        name = str(self._cfg.get("preference_adapter", "") or "").strip()
        resolver = self._cfg.get("adapter_resolver")
        if not name or not self._cfg.get("reverse_audit", False):
            self._preference_adapter = None
            return None
        if not callable(resolver):
            logger.warning(
                "memlock: no adapter resolver wired; "
                "adapter '%s' disabled", name,
            )
            self._preference_adapter = None
            return None
        try:
            self._preference_adapter = resolver(name)
        except Exception as exc:
            logger.warning(
                "memlock: adapter '%s' failed to load: %s; reverse audit disabled",
                name, exc,
            )
            self._preference_adapter = None
        return self._preference_adapter

    def reset_preference_adapter_cache(self) -> None:
        """Test/config-reload hook: force re-resolution on the next audit."""
        self._preference_adapter = self._UNSET

    def run_reverse_audit(self, active_region: list[dict]) -> tuple[list[dict], dict]:
        """Query the configured adapter; return budget-ready preference reminders."""
        provider = self._resolve_preference_provider()
        if not self._cfg.get("reverse_audit", False) or provider is None:
            return [], {
                "status": "ok", "findings": [], "rehydrate_ids": [],
                "suppressed_ids": [], "errors": [],
            }
        try:
            rows = provider(
                str(self._cfg.get(
                    "reverse_preference_query", "applicable user preferences"
                )),
                int(self._cfg.get("reverse_limit", 50)),
            )
            if not isinstance(rows, list):
                raise TypeError("preference provider must return a list")
            report: dict = reverse_audit(  # type: ignore[assignment]
                rows, active_region,
                now=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                drift_threshold=float(self._cfg.get("drift_threshold", 0.5)),
            )
        except Exception as exc:
            logger.warning("memlock: reverse preference query unavailable")
            return [], reverse_audit_unavailable(exc)
        by_id = {
            f["canonical_id"]: f for f in report["findings"] if f["canonical_id"]
        }
        candidates = []
        for memory_id in report["rehydrate_ids"]:
            finding = by_id[memory_id]
            row = next((item for item in rows if item.get("id") == memory_id), {})
            metadata = row.get("metadata_json", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (TypeError, ValueError):
                    metadata = {}
            ml = metadata.get("memlock", {}) if isinstance(metadata, dict) else {}
            candidates.append({
                "id": memory_id, "text": finding["preference"],
                "reminder": finding["preference"][:120],
                "priority": int(ml.get("priority", 50)) if isinstance(ml, dict) else 50,
            })
        return candidates, report

    # ── per-session store access ────────────────────────────────────────

    def get_store(self, session_id: str) -> SessionStore | None:
        """Return the SessionStore for this session_id, or None if not initialised."""
        return self.stores.get(session_id)

    def ensure_store(self, session_id: str) -> SessionStore:
        """Return or create the SessionStore for this session_id."""
        if session_id not in self.stores:
            self.stores[session_id] = SessionStore(session_id)
        return self.stores[session_id]

    def get_durable(self) -> Any:
        """Lazy-init the durable pin store from config."""
        if self.durable_store is None:
            backend = str(self._cfg.get("persistence_backend", "file"))
            self.durable_store = _get_durable_store(backend)
        return self.durable_store

    # ── lifecycle hooks ─────────────────────────────────────────────────

    def on_start(self, session_id: str = "", **kwargs) -> None:
        if not session_id:
            return
        self.current_session_id = session_id
        store = self.ensure_store(session_id)
        self.session_turns[session_id] = 0

        # Seed static anchors from config
        static_anchors = self._cfg.get("anchors", [])
        if static_anchors:
            valid = self.validate_anchors(static_anchors)
            for a in valid:
                if a["id"] not in store.anchors():
                    store.add_anchor(
                        anchor_id=a["id"],
                        text=a["text"],
                        reminder=a["reminder"],
                        priority=a["priority"],
                        probes=a["probes"],
                        pinned=a.get("pinned", False),
                    )

        # Seed global pins from durable store (cross-session persistence)
        try:
            durable = self.get_durable()
            global_pins = durable.load_pins()
            for pin in global_pins:
                if pin.get("scope") != "global":
                    continue
                if pin["id"] in store.anchors():
                    continue
                store.add_anchor(
                    anchor_id=pin["id"],
                    text=pin["text"],
                    reminder=pin.get("reminder", pin["text"][:120]),
                    priority=pin.get("priority", 50),
                    probes=pin.get("probes", []),
                    pinned=True,
                )
        except Exception as exc:
            logger.warning("memlock: failed to seed global pins: %s", exc)

    def on_end(self, session_id: str = "", **kwargs) -> None:
        """Save the store for this session_id for durability (fired every turn)."""
        if not session_id:
            return
        store = self.get_store(session_id)
        if store is None:
            return
        try:
            store.save()
        except Exception as exc:
            logger.warning(
                "memlock: on_session_end save failed for %s: %s",
                session_id, exc,
            )

    def pre_llm_turn(
        self,
        session_id: str = "",
        turn_id: str = "",
        user_message: str = "",
        conversation_history: list | None = None,
        **kwargs,
    ) -> dict | str | None:
        """Audit anchors post-compaction, rehydrate casualties.

        Harness-neutral pre-LLM turn hook: the host shim calls this with the
        conversation it is about to send to the model.
        """
        if not session_id:
            return None
        self.current_session_id = session_id

        store = self.ensure_store(session_id)

        # Per-session turn counter
        self.session_turns.setdefault(session_id, 0)
        self.session_turns[session_id] += 1
        turn = self.session_turns[session_id]

        if conversation_history is None:
            conversation_history = []

        # ── detect compaction ───────────────────────────────────────────
        summary_idx, summary_body = find_summary(
            conversation_history, prefixes=self.summary_prefixes
        )
        summary_hash = hash_summary_body(summary_body)

        compaction_event = store.is_new_compaction(summary_hash)
        if compaction_event and summary_hash is not None:
            store.record_compaction(summary_hash, turn)

        # ── safety net (no compaction, but many turns since reinjection) ──
        hard_reinject_turns = int(self._cfg.get("hard_reinject_turns", 40))
        safety_net = (
            not compaction_event
            and hard_reinject_turns > 0
            and (turn - store.last_reinject_turn) >= hard_reinject_turns
        )

        # 'always' injects every turn; 'on-drift' only audits and injects on
        # compaction or the safety net.
        inject_mode = str(self._cfg.get("inject", "on-drift"))
        should_audit = compaction_event or safety_net

        if inject_mode != "always" and not should_audit:
            return None

        anchors = store.sorted_anchors()
        reverse_candidates: list[dict] = []
        reverse_report: dict | None = None

        # ── audit (drift state and /guard score; gating only in on-drift) ──
        alive_ids: list[str] = []
        drifted_ids: list[str] = []
        if should_audit:
            active_region, _ = split_context(conversation_history, summary_idx)
            if user_message and not any(
                str(msg.get("role", "")).lower() == "user"
                and str(msg.get("content", "")) == user_message
                for msg in active_region
            ):
                active_region.append({"role": "user", "content": user_message})
            reverse_candidates, reverse_report = self.run_reverse_audit(active_region)
            detection_mode = self._cfg.get("detection", "keyword")

            if detection_mode == "semantic" and anchors:
                sim_threshold = float(self._cfg.get("sim_threshold", 0.65))
                model_name = str(self._cfg.get("embedding_model", "all-MiniLM-L6-v2"))
                window_chars = int(self._cfg.get("semantic_window_chars", 1000))
                alive_ids, drifted_ids = semantic_audit_anchors(
                    anchors, active_region, model_name=model_name,
                    sim_threshold=sim_threshold, window_chars=window_chars,
                )
            elif anchors:
                alive_ids, drifted_ids = audit_anchors(
                    anchors, active_region,
                    drift_threshold=float(self._cfg.get("drift_threshold", 0.5)),
                )

            for aid in alive_ids:
                store.mark_anchor_alive(aid, turn)
            for did in drifted_ids:
                store.mark_anchor_drifted(did)

            score = store.compute_integrity_score()
            reverse_diagnostics = None
            if reverse_report is not None:
                reverse_diagnostics = {
                    "absent": [f["canonical_id"] for f in reverse_report["findings"]
                               if f["verdict"] == "ABSENT" and f["canonical_id"]],
                    "contradicted": [mid for f in reverse_report["findings"]
                                     if f["verdict"] == "CONTRADICTED"
                                     for mid in f["memory_ids"]],
                    "stale": [mid for f in reverse_report["findings"]
                              if f["verdict"] == "STORED_STALE"
                              for mid in f["memory_ids"]],
                    "unknown": [mid for f in reverse_report["findings"]
                                if f["verdict"] == "UNKNOWN"
                                for mid in f["memory_ids"]],
                }
            store.log_drift(drifted_ids, score, reverse=reverse_diagnostics)

            # ── alert ────────────────────────────────────────────────
            alert_floor = int(self._cfg.get("alert_floor", 70))
            alert_cooldown = float(self._cfg.get("alert_cooldown_s", 1800))
            if score >= 0 and score < alert_floor and store.can_alert(alert_cooldown):
                alert_msg = (
                    f"[memlock] integrity score {score}% "
                    f"(session {session_id})"
                )
                if drifted_ids:
                    alert_msg += f" — drifted: {', '.join(drifted_ids[:5])}"
                logger.warning(alert_msg)
                # Optional shell-out
                script = self._cfg.get("alert_script", "")
                if script:
                    try:
                        import subprocess

                        subprocess.Popen(
                            [script, alert_msg],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except Exception as exc:
                        logger.warning("memlock: alert script failed: %s", exc)
                store.record_alert()

        # ── rehydrate ───────────────────────────────────────────────
        # Candidate selection; slot and char budgets in select_casualties cut.
        all_ids = [a["id"] for a in anchors]
        rehydrate_ids: list[str] = []
        if inject_mode == "always":
            rehydrate_ids = all_ids
        elif drifted_ids:
            rehydrate_ids = drifted_ids
        elif safety_net:
            rehydrate_ids = all_ids
        elif not reverse_candidates:
            return None

        max_slots = int(self._cfg.get("max_slots", 8))
        max_chars = int(self._cfg.get("max_reminder_chars", 600))
        selected, remaining = self.select_casualties(
            store, rehydrate_ids, max_slots, max_chars
        )
        total_chars = sum(len(item.get("reminder", "")[:120]) + 4 for item in selected)
        for candidate in sorted(
            reverse_candidates, key=lambda item: (-item["priority"], item["id"])
        ):
            reminder_chars = len(candidate["reminder"][:120]) + 4
            if len(selected) >= max_slots or total_chars + reminder_chars > max_chars:
                remaining.append(candidate["id"])
                continue
            selected.append(candidate)
            total_chars += reminder_chars

        reminder_block = self.build_reminder_block(selected, remaining)
        if reminder_block is None:
            return None

        store.set_reinject_turn(turn)

        # Return context dict for the shim to deliver to its harness.
        return {"context": reminder_block}

    # ── guard_pin lifecycle ─────────────────────────────────────────────

    def pin_handler(self, args: dict | None = None, **kwargs) -> str:
        """Tool handler for the guard_pin tool — dispatches pin/update/unpin.

        Reads ``session_id`` from ``kwargs``, falling back to the last-seen
        session for hosts that do not forward it (documented race, see
        docs/optional-dispatch-patch.md).
        """
        if args is None:
            args = {}

        session_id = kwargs.get("session_id", "") or self.current_session_id
        if not session_id:
            return "Error: no active session; cannot pin before a session starts"
        store = self.ensure_store(session_id)

        unpin_id = str(args.get("unpin", "")).strip()
        if unpin_id:
            return self.unpin(store, unpin_id, args)
        pin_id = str(args.get("pin_id", "")).strip()
        if str(args.get("action", "")).strip().lower() == "rollback":
            return self.rollback_pin(store, pin_id, args)
        if pin_id:
            return self.update_pin(store, pin_id, args)
        return self.do_pin(store, args)

    def do_pin(self, store: SessionStore, args: dict) -> str:
        # Flatten whitespace: newlines in pinned text could otherwise spoof
        # extra list items or a second marker line inside the reminder block.
        text = re.sub(r"\s+", " ", str(args.get("text", ""))).strip()
        if not text:
            return "Error: 'text' is required for pin"

        max_pins = int(self._cfg.get("max_pins", 16))
        pinned_now = sum(1 for a in store.anchors().values() if a["pinned"])
        if pinned_now >= max_pins:
            return (
                f"Error: pin limit reached ({max_pins}). "
                f"Unpin something first (see /guard)."
            )

        reminder = re.sub(r"\s+", " ", str(args.get("reminder", ""))).strip()
        if not reminder:
            reminder = re.split(r"[.!?]\s+", text)[0][:120]

        priority = max(1, min(100, int(args.get("priority", 50))))
        probes = args.get("probes", [])
        if not probes:
            probes = _derive_probes(text)

        anchor_id = f"pin_{int(time.time())}_{len(store.anchors())}"
        scope = str(args.get("scope", "session")).strip().lower()
        if scope not in ("session", "global"):
            scope = "session"

        store.add_anchor(
            anchor_id=anchor_id,
            text=text,
            reminder=reminder,
            priority=priority,
            probes=[str(p) for p in probes],
            pinned=True,
        )

        # Persist global-scoped pins to durable store
        if scope == "global":
            try:
                durable = self.get_durable()
                durable.save_pin({
                    "id": anchor_id,
                    "text": text,
                    "reminder": reminder,
                    "priority": priority,
                    "probes": [str(p) for p in probes],
                    "scope": "global",
                    "pinned_at": time.time(),
                })
            except Exception as exc:
                logger.warning("memlock: failed to persist global pin: %s", exc)

        scope_note = " (global — survives sessions)" if scope == "global" else ""
        return (
            f"Pinned instruction (id={anchor_id}, priority={priority}, "
            f"probes={len(probes)}, scope={scope}{scope_note}):\n  {text}\n"
            f"Will survive context compaction."
        )

    def update_pin(self, store: SessionStore, pin_id: str, args: dict) -> str:
        """Update an existing pin in place, keeping the same anchor id.

        Newline flattening, reminder auto-trim and priority clamping apply to
        the updated text exactly as for a new pin. Global-scoped pins are
        re-saved to the durable store (save_pin upserts by id). A missing id
        is a clear error listing the first available pin ids.
        """
        existing = store.get_anchor(pin_id)
        if existing is None:
            available = [
                a["id"] for a in store.sorted_anchors() if a["pinned"]
            ][:8]
            listed = ", ".join(available) if available else "(none)"
            return (
                f"Error: pin '{pin_id}' not found. "
                f"Available pin ids: {listed}"
            )

        # Same normalisation rules as do_pin — updated text must not be able
        # to do anything a new pin cannot (no reminder-block spoofing via
        # newlines).
        text = re.sub(r"\s+", " ", str(args.get("text", ""))).strip()
        if not text:
            return "Error: 'text' is required for update"

        reminder = re.sub(r"\s+", " ", str(args.get("reminder", ""))).strip()
        if not reminder:
            reminder = re.split(r"[.!?]\s+", text)[0][:120]

        priority = max(1, min(100, int(args.get("priority", existing["priority"]))))
        probes = args.get("probes", [])
        if not probes:
            probes = _derive_probes(text)

        if not store.update_anchor(
            pin_id, text=text, reminder=reminder,
            priority=priority, probes=[str(p) for p in probes],
        ):
            return f"Error: pin '{pin_id}' not found"

        # If this id exists in the durable store it was a global pin: keep
        # the durable copy in sync. save_pin upserts by id; session-only pins
        # are untouched there.
        durable_note = ""
        try:
            durable = self.get_durable()
            known = {p.get("id") for p in durable.load_pins()}
            if pin_id in known:
                durable.save_pin({
                    "id": pin_id,
                    "text": text,
                    "reminder": reminder,
                    "priority": priority,
                    "probes": [str(p) for p in probes],
                    "scope": "global",
                    "pinned_at": time.time(),
                })
                durable_note = " (global copy updated)"
        except Exception as exc:
            logger.warning("memlock: failed to persist global pin update: %s", exc)

        return (
            f"Updated pin (id={pin_id}, priority={priority}, "
            f"probes={len(probes)}{durable_note}):\n  {text}\n"
            f"Previous version kept in history."
        )

    def rollback_pin(
        self, store: SessionStore, pin_id: str, args: dict | None = None,
    ) -> str:
        """Restore a prior version of a pin from its history (ChronoMem).

        Selection semantics: ``args['version']`` is an optional index into
        the anchor's history list; the default (-1, or any negative index)
        selects the MOST RECENT previous version, 0 the oldest retained.
        The displaced CURRENT state is pushed onto history first — rollback
        destroys nothing and remains itself rollback-able. Global-scoped
        pins are re-persisted to the durable store so future sessions seed
        the restored wording.
        """
        existing = store.get_anchor(pin_id)
        if existing is None:
            available = [
                a["id"] for a in store.sorted_anchors() if a["pinned"]
            ][:8]
            listed = ", ".join(available) if available else "(none)"
            return (
                f"Error: pin '{pin_id}' not found. "
                f"Available pin ids: {listed}"
            )
        history = existing.get("history") or []
        if not history:
            return (
                f"Error: pin '{pin_id}' has no version history to roll back to. "
                f"Update it first (guard_pin with pin_id + text)."
            )
        try:
            version = int((args or {}).get("version", -1))
        except (TypeError, ValueError):
            return "Error: 'version' must be an integer index into the pin's history"

        if not store.rollback_anchor(pin_id, version=version):
            return (
                f"Error: version index {version} out of range for pin "
                f"'{pin_id}' ({len(history)} version(s) in history; use -1 "
                f"for the most recent previous version)."
            )

        restored = store.get_anchor(pin_id) or {}
        # Re-persist when this id exists in the durable store (global pin);
        # save_pin upserts by id, session-only pins are untouched there.
        durable_note = ""
        try:
            durable = self.get_durable()
            known = {p.get("id") for p in durable.load_pins()}
            if pin_id in known:
                durable.save_pin({
                    "id": pin_id,
                    "text": restored.get("text", ""),
                    "reminder": restored.get(
                        "reminder", restored.get("text", "")[:120],
                    ),
                    "priority": restored.get("priority", 50),
                    "probes": list(restored.get("probes") or []),
                    "scope": "global",
                    "pinned_at": time.time(),
                })
                durable_note = " (global copy rolled back)"
        except Exception as exc:
            logger.warning("memlock: failed to persist global pin rollback: %s", exc)

        return (
            f"Rolled back pin (id={pin_id}, priority={restored.get('priority', 50)}"
            f"{durable_note}):\n  {restored.get('text', '')}\n"
            f"Displaced version kept in history."
        )

    def unpin(self, store: SessionStore, anchor_id: str,
              args: dict | None = None) -> str:
        """Remove a pinned anchor by id.

        Scope semantics for durable (global) pins:
          - default / scope="session": remove only THIS session's copy; the
            durable copy stays so other sessions keep the pin.
          - args["scope"]="global": also remove the durable copy, so future
            sessions stop seeding it.
        """
        ok = store.unpin(anchor_id)
        if not ok:
            return f"Error: anchor '{anchor_id}' not found or not pinned"

        try:
            durable = self.get_durable()
        except Exception as exc:
            logger.warning("memlock: failed to open durable store for unpin: %s", exc)
            return f"Unpinned: {anchor_id}"

        if str((args or {}).get("scope", "session")).strip().lower() == "global":
            try:
                durable.remove_pin(anchor_id)
            except Exception as exc:
                # Best-effort; the session copy is already gone.
                logger.warning("memlock: failed to remove durable pin: %s", exc)
            return f"Unpinned globally: {anchor_id}"
        return f"Unpinned: {anchor_id}"

    # ── status (/guard equivalent) ───────────────────────────────────────

    def status_text(self, raw_args: str = "", session_id: str = "") -> str:
        """Status command body — integrity score, anchor status, drift log."""
        sid = session_id or self.current_session_id
        if not sid:
            return "MemLock: no active session yet"
        store = self.ensure_store(sid)

        score = store.integrity_score
        anchors = store.sorted_anchors()

        lines = [
            f"MemLock — session {sid}",
            f"  integrity_score: {score}% (anchors: {len(anchors)})",
            f"  pins: {sum(1 for a in anchors if a['pinned'])}",
            f"  static: {sum(1 for a in anchors if not a['pinned'])}",
            f"  last compaction: {store._data.get('last_compaction_at', 'never')}",
            "",
        ]

        if anchors:
            lines.append("Anchors:")
            for a in anchors:
                status = "ALIVE" if not a["drifted"] else "DRIFTED"
                kind = "[pin]" if a["pinned"] else "[static]"
                lines.append(
                    f"  {kind} [{status}] {a['id']} "
                    f"(p={a['priority']}) — {a.get('reminder', '')[:60]}"
                )

        # Compact per-pin version history (ChronoMem): id, version count,
        # current text head. Only pins that actually HAVE history appear.
        pinned_with_history = [
            a for a in anchors if a["pinned"] and (a.get("history") or [])
        ]
        if pinned_with_history:
            lines.append("Pin version history (oldest -> newest, then current):")
            for a in pinned_with_history:
                versions = a["history"]
                heads = " | ".join(
                    f"{h.get('text', '')[:30]}" for h in versions
                )
                lines.append(
                    f"  {a['id']} ({len(versions)} prior version(s)) "
                    f"current: {a.get('text', '')[:60]}"
                )
                lines.append(f"    history: {heads}")

        drift_log = store._data.get("drift_log", [])
        if drift_log:
            lines.append(f"\nDrift events: {len(drift_log)} (most recent first)")
            for event in reversed(drift_log[-3:]):
                when = time.strftime("%H:%M:%S", time.localtime(event["time"]))
                lines.append(
                    f"  {when} score={event['score']}% "
                    f"casualties={len(event['casualties'])}"
                )

        return "\n".join(lines)


def load_config_file(path: Path) -> dict:
    """Best-effort YAML/JSON config read. Missing/unparseable → {} (fail-open).

    Uses PyYAML when importable; falls back to JSON so stdlib-only hosts can
    still supply configuration. Never raises.
    """
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    try:
        import yaml  # type: ignore[import-untyped]

        raw = yaml.safe_load(text)
        return dict(raw.get("memlock", {})) if isinstance(raw, dict) else {}
    except ImportError:
        try:
            raw = json.loads(text)
            return dict(raw.get("memlock", {})) if isinstance(raw, dict) else {}
        except (TypeError, ValueError):
            return {}
    except Exception:
        return {}
