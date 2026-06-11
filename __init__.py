"""MemLock — Re-assert standing instructions after context compaction.

Plugin for Hermes Agent.  Detects compaction events via SUMMARY_PREFIX scan,
audits which pinned anchors survived in the active (non-summary) region,
and rehydrates casualty reminders into the user turn.

Detection modes: keyword probes (default) or windowed semantic similarity
(optional, needs sentence-transformers).  Injection modes: on-drift (default)
or always.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    from .detection import (
        audit_anchors,
        find_summary,
        hash_summary_body,
        semantic_audit_anchors,
        split_context,
    )
    from .store import SessionStore
except ImportError:
    # Loaded as a plain module (plugin loaders that exec the file, pytest)
    from detection import (  # type: ignore[no-redef]
        audit_anchors,
        find_summary,
        hash_summary_body,
        semantic_audit_anchors,
        split_context,
    )
    from store import SessionStore  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Per-session stores keyed by session_id.  No cross-session leakage.
# Tool handler binds to _current_session_id (last-seen session), documented race.
_stores: dict[str, SessionStore] = {}
_session_turns: dict[str, int] = {}
_current_session_id: str = ""
_cfg: dict[str, Any] = {}

# Stable marker for reminder blocks.  Phrasing rules:
#  - declarative, innocuous, no urgency theatre
#  - no self-concealing language
#  - stable wording for cache-friendliness
REMINDER_MARKER = "[Standing instructions — still active]"

_CWD = Path(__file__).resolve().parent
_LOCAL_CFG = _CWD / "config.yaml"


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
        import yaml

        hp = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        cf = hp / "config.yaml"
        if cf.exists():
            raw = yaml.safe_load(cf.read_text())
            return dict(raw.get("memlock", {}))
    except Exception:
        pass
    return {}


def _load_defaults() -> dict:
    try:
        import yaml

        raw = yaml.safe_load(_LOCAL_CFG.read_text())
        return dict(raw.get("memlock", {}))
    except Exception:
        return {}


def _merge_cfg(user_cfg: dict) -> dict:
    defaults = _load_defaults()
    defaults.update(user_cfg)
    return defaults


def _validate_anchors(anchors: list[dict]) -> list[dict]:
    """Validate static anchors from config.  Drops any that fail validation.

    Probe-less anchors cannot be audited individually, so they are only
    accepted when they are the sole anchor defined.  The rule is applied
    against the total config count, not insertion order, so the same set is
    accepted or rejected identically regardless of ordering.
    """
    # Count only structurally valid anchors: a probe-less anchor that would
    # end up the sole survivor after filtering must still be accepted.
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


# ── rehydration ─────────────────────────────────────────────────────────


def _select_casualties(
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


def _build_reminder_block(
    selected: list[dict],
    remaining: list[str],
) -> str | None:
    """Build the reminder injection string.  Returns None if nothing to inject."""
    if not selected:
        return None
    lines = [REMINDER_MARKER]
    for a in selected:
        reminder = a.get("reminder", a.get("text", ""))[:120]
        lines.append(f"  - {reminder}")
    if remaining:
        lines.append(
            f"  - ({len(remaining)} additional instruction(s) not shown — "
            f"the user can run /guard to see the full list)"
        )
    return "\n".join(lines)


# ── per-session store access ────────────────────────────────────────────


def _get_store(session_id: str) -> SessionStore | None:
    """Return the SessionStore for this session_id, or None if not initialised."""
    return _stores.get(session_id)


def _ensure_store(session_id: str) -> SessionStore:
    """Return or create the SessionStore for this session_id."""
    if session_id not in _stores:
        _stores[session_id] = SessionStore(session_id)
    return _stores[session_id]


# ── hooks ───────────────────────────────────────────────────────────────


def _on_start(session_id: str = "", **kwargs) -> None:
    global _current_session_id
    if not session_id:
        return
    _current_session_id = session_id
    store = _ensure_store(session_id)
    _session_turns[session_id] = 0

    # Seed static anchors from config
    static_anchors = _cfg.get("anchors", [])
    if static_anchors:
        valid = _validate_anchors(static_anchors)
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


def _on_end(session_id: str = "", **kwargs) -> None:
    """Save the store for this session_id for durability (fired every turn)."""
    if not session_id:
        return
    store = _get_store(session_id)
    if store is None:
        return
    try:
        store.save()
    except Exception as exc:
        logger.warning(
            "memlock: on_session_end save failed for %s: %s",
            session_id, exc,
        )


def _on_pre_llm(
    session_id: str = "",
    turn_id: str = "",
    user_message: str = "",
    conversation_history: list | None = None,
    **kwargs,
) -> dict | str | None:
    """Audit anchors post-compaction, rehydrate casualties."""
    global _current_session_id, _session_turns

    if not session_id:
        return None
    _current_session_id = session_id

    store = _ensure_store(session_id)

    # Per-session turn counter
    _session_turns.setdefault(session_id, 0)
    _session_turns[session_id] += 1
    turn = _session_turns[session_id]

    if conversation_history is None:
        conversation_history = []

    # ── detect compaction ───────────────────────────────────────────
    summary_idx, summary_body = find_summary(conversation_history)
    summary_hash = hash_summary_body(summary_body)

    compaction_event = store.is_new_compaction(summary_hash)
    if compaction_event and summary_hash is not None:
        store.record_compaction(summary_hash, turn)

    # ── safety net (no compaction, but many turns since reinjection) ──
    hard_reinject_turns = int(_cfg.get("hard_reinject_turns", 40))
    safety_net = (
        not compaction_event
        and hard_reinject_turns > 0
        and (turn - store.last_reinject_turn) >= hard_reinject_turns
    )

    # 'always' injects every turn; 'on-drift' only audits and injects on
    # compaction or the safety net.
    inject_mode = str(_cfg.get("inject", "on-drift"))
    should_audit = compaction_event or safety_net

    if inject_mode != "always" and not should_audit:
        return None

    anchors = store.sorted_anchors()
    if not anchors:
        return None

    # ── audit (drift state and /guard score; gating only in on-drift) ──
    drifted_ids: list[str] = []
    if should_audit:
        active_region, _ = split_context(conversation_history, summary_idx)
        detection_mode = _cfg.get("detection", "keyword")
        drift_threshold = float(_cfg.get("drift_threshold", 0.5))

        if detection_mode == "semantic":
            sim_threshold = float(_cfg.get("sim_threshold", 0.65))
            model_name = str(_cfg.get("embedding_model", "all-MiniLM-L6-v2"))
            window_chars = int(_cfg.get("semantic_window_chars", 1000))
            alive_ids, drifted_ids = semantic_audit_anchors(
                anchors, active_region, model_name=model_name,
                sim_threshold=sim_threshold, window_chars=window_chars,
            )
        else:
            alive_ids, drifted_ids = audit_anchors(
                anchors, active_region, drift_threshold=drift_threshold
            )

        for aid in alive_ids:
            store.mark_anchor_alive(aid, turn)
        for did in drifted_ids:
            store.mark_anchor_drifted(did)

        score = store.compute_integrity_score()
        store.log_drift(drifted_ids, score)

        # ── alert ───────────────────────────────────────────────────
        alert_floor = int(_cfg.get("alert_floor", 70))
        alert_cooldown = float(_cfg.get("alert_cooldown_s", 1800))
        if score >= 0 and score < alert_floor and store.can_alert(alert_cooldown):
            alert_msg = (
                f"[memlock] integrity score {score}% "
                f"(session {session_id})"
            )
            if drifted_ids:
                alert_msg += f" — drifted: {', '.join(drifted_ids[:5])}"
            logger.warning(alert_msg)
            # Optional shell-out
            script = _cfg.get("alert_script", "")
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

    # ── rehydrate ───────────────────────────────────────────────────
    # Candidate selection; slot and char budgets in _select_casualties cut.
    all_ids = [a["id"] for a in anchors]
    if inject_mode == "always":
        rehydrate_ids = all_ids
    elif drifted_ids:
        rehydrate_ids = drifted_ids
    elif safety_net:
        rehydrate_ids = all_ids
    else:
        return None

    max_slots = int(_cfg.get("max_slots", 8))
    max_chars = int(_cfg.get("max_reminder_chars", 600))
    selected, remaining = _select_casualties(
        store, rehydrate_ids, max_slots, max_chars
    )

    reminder_block = _build_reminder_block(selected, remaining)
    if reminder_block is None:
        return None

    store.set_reinject_turn(turn)

    # Return context dict — appended to plugin_user_context in turn_context.py
    return {"context": reminder_block}


# ── tool: guard_pin ────────────────────────────────────────────────────

_PIN_SCHEMA = {
    "name": "guard_pin",
    "description": (
        "Pin a standing instruction that must survive context compaction. "
        "Use 'text' to pin a new instruction. Use 'unpin' with an anchor id "
        "to remove a previously pinned instruction."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The standing instruction to preserve (for pin).",
            },
            "unpin": {
                "type": "string",
                "description": "Anchor id to remove (for unpin).",
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
        },
        "required": [],
    },
}


def _derive_probes(text: str) -> list[str]:
    """Auto-derive probes from text using distinctive tokens."""
    common = {
        "that", "this", "from", "with", "your", "will", "when",
        "they", "have", "been", "were", "their", "about", "would",
        "which", "there", "should", "could", "these", "those",
    }
    tokens = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    distinctive = [t for t in tokens if t not in common]
    seen: set[str] = set()
    probes: list[str] = []
    for t in distinctive:
        if t not in seen:
            seen.add(t)
            probes.append(t)
    return probes[:5]


def _pin_handler(args: dict | None = None, **kwargs) -> str:
    """Tool handler for guard_pin — dispatches to _do_pin or _do_unpin.

    Reads ``session_id`` from ``kwargs`` (forwarded by the tool dispatch
    layer). Falls back to ``_current_session_id`` for compatibility with
    vanilla Hermes that doesn't forward session_id.
    """
    if args is None:
        args = {}

    # Prefer forwarded session_id; fall back to last-seen global
    session_id = kwargs.get("session_id", "") or _current_session_id
    if not session_id:
        return "Error: no active session; cannot pin before a session starts"
    store = _ensure_store(session_id)

    unpin_id = str(args.get("unpin", "")).strip()
    if unpin_id:
        return _do_unpin(store, unpin_id)
    return _do_pin(store, args)


def _do_pin(store: SessionStore, args: dict) -> str:
    # Flatten whitespace: newlines in pinned text could otherwise spoof
    # extra list items or a second marker line inside the reminder block.
    text = re.sub(r"\s+", " ", str(args.get("text", ""))).strip()
    if not text:
        return "Error: 'text' is required for pin"

    max_pins = int(_cfg.get("max_pins", 16))
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

    store.add_anchor(
        anchor_id=anchor_id,
        text=text,
        reminder=reminder,
        priority=priority,
        probes=[str(p) for p in probes],
        pinned=True,
    )

    return (
        f"Pinned instruction (id={anchor_id}, priority={priority}, "
        f"probes={len(probes)}):\n  {text}\n"
        f"Will survive context compaction."
    )


def _do_unpin(store: SessionStore, anchor_id: str) -> str:
    """Remove a pinned anchor by id."""
    ok = store.unpin(anchor_id)
    if ok:
        return f"Unpinned: {anchor_id}"
    return f"Error: anchor '{anchor_id}' not found or not pinned"


# ── slash command ───────────────────────────────────────────────────────


def _status_cmd(raw_args: str = "") -> str:
    """Handle /guard slash command — current integrity score and anchor status."""
    global _current_session_id

    if not _current_session_id:
        return "MemLock: no active session yet"
    store = _ensure_store(_current_session_id)

    score = store.integrity_score
    anchors = store.sorted_anchors()

    lines = [
        f"MemLock — session {_current_session_id}",
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


# ── registration ────────────────────────────────────────────────────────


def register(ctx) -> None:
    global _cfg
    _cfg = _merge_cfg(_safe_cfg(ctx))

    ctx.register_hook("on_session_start", _on_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm)
    ctx.register_hook("on_session_end", _on_end)

    # guard_pin tool — single tool with pin/unpin via args
    ctx.register_tool(
        name="guard_pin",
        toolset="guard",
        description=(
            "Pin a standing instruction that must survive context compaction. "
            "Use 'text' to pin.  Use 'unpin' with an anchor id to remove."
        ),
        handler=_pin_handler,
        schema=_PIN_SCHEMA,
    )

    ctx.register_command(
        name="guard",
        handler=_status_cmd,
        description="MemLock status: integrity score, anchors, drift log",
    )
