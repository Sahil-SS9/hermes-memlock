"""Compaction detection and keyword probe audit for MemLock.

Compaction: scan conversation_history for the compressor's SUMMARY_PREFIX
literal.  Hash the summary body; new/changed hash = compaction event.

Keyword probe: scope to the non-summary region only.  An anchor's probes
hitting only inside the summary block are NOT survival — the SUMMARY_PREFIX
demotes that text to background.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

logger = logging.getLogger(__name__)

# Import the compressor constants.  Fall back to frozen literals if the import
# fails (vanilla Hermes may not expose the module).
try:
    from agent.context_compressor import (  # type: ignore[import-untyped]
        SUMMARY_PREFIX,
        LEGACY_SUMMARY_PREFIX,
        _HISTORICAL_SUMMARY_PREFIXES,
    )
except ImportError:
    SUMMARY_PREFIX = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below."
    )
    LEGACY_SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"
    _HISTORICAL_SUMMARY_PREFIXES: tuple[str, ...] = ()

# All known summary prefixes in detection priority order.
_SUMMARY_PREFIXES: list[str] = [
    SUMMARY_PREFIX,
    LEGACY_SUMMARY_PREFIX,
    *_HISTORICAL_SUMMARY_PREFIXES,
]


def _message_text(msg: dict) -> str:
    """Normalise a conversation message into a string for detection."""
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    if not isinstance(content, str):
        return str(content)
    return content


def find_summary(
    conversation_history: list[dict],
) -> tuple[int | None, str | None]:
    """Find the summary message in conversation_history.

    Returns (idx, summary_body).
      idx          — index of the summary message, or None if not found.
      summary_body — the text after the prefix, or None.

    Scans from the front (system prompt is at idx 0, oldest messages next).
    The summary, if present, is typically near the front after system.
    """
    for i, msg in enumerate(conversation_history):
        text = _message_text(msg)
        for prefix in _SUMMARY_PREFIXES:
            if text.startswith(prefix):
                body = text[len(prefix):].strip()
                return i, body
    return None, None


def hash_summary_body(body: str | None) -> str | None:
    if body is None:
        return None
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def split_context(
    conversation_history: list[dict],
    summary_idx: int | None,
) -> tuple[list[dict], list[dict]]:
    """Split conversation_history into summary region and active region.

    If summary_idx is None, everything is active.

    Returns (active_region, summary_region).
      active_region  — non-summary messages (the live context).
      summary_region — the summary message plus any preceding messages
                       that are not system prompt (idx 0).
    """
    if summary_idx is None:
        return list(conversation_history), []

    active: list[dict] = []
    summary_region: list[dict] = []

    for i, msg in enumerate(conversation_history):
        # idx 0 is system prompt — always active
        if i == 0:
            active.append(msg)
            continue
        if i <= summary_idx:
            summary_region.append(msg)
        else:
            active.append(msg)
    return active, summary_region


def _probe_hit(text: str, probes: list[str]) -> int:
    """Count how many probes appear (case-insensitive substring) in text.

    Returns integer count, not fraction.
    """
    lower = text.lower()
    hits = 0
    for probe in probes:
        if probe.lower() in lower:
            hits += 1
    return hits


ReverseVerdict = Literal[
    "PRESENT", "ABSENT", "CONTRADICTED", "STORED_STALE", "AMBIGUOUS", "UNKNOWN"
]


class PreferenceMemory(TypedDict, total=False):
    id: str
    content: str
    source: str | None
    timestamp: str | None
    created_at: str | None
    valid_until: str | None
    superseded_by: str | None
    scope: str | None
    metadata_json: str | dict | None
    veracity: str | None
    memory_type: str | None


class ReverseFinding(TypedDict):
    memory_ids: list[str]
    canonical_id: str | None
    preference: str
    verdict: ReverseVerdict
    material: bool
    evidence: list[str]
    reason: str
    action: Literal["none", "rehydrate", "suppress", "review"]


class ReverseAuditReport(TypedDict):
    status: Literal["ok", "degraded", "unavailable"]
    findings: list[ReverseFinding]
    rehydrate_ids: list[str]
    suppressed_ids: list[str]
    errors: list[dict[str, str]]


def _derive_probes(text: str) -> list[str]:
    """Derive stable keyword probes for preferences without explicit probes."""
    common = {
        "that", "this", "from", "with", "your", "will", "when", "they",
        "have", "been", "were", "their", "about", "would", "which", "there",
        "should", "could", "these", "those",
    }
    tokens = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    return list(dict.fromkeys(t for t in tokens if t not in common))[:5]


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _metadata(row: PreferenceMemory) -> tuple[dict[str, Any], bool]:
    raw = row.get("metadata_json")
    if raw in (None, ""):
        return {}, True
    if isinstance(raw, dict):
        return raw, True
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            return (decoded, True) if isinstance(decoded, dict) else ({}, False)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, False
    return {}, False


def _memlock_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    nested = metadata.get("memlock", {})
    return nested if isinstance(nested, dict) else {}


def _finding(
    ids: list[str], canonical_id: str | None, preference: str,
    verdict: ReverseVerdict, material: bool, evidence: list[str], reason: str,
    action: Literal["none", "rehydrate", "suppress", "review"],
) -> ReverseFinding:
    return {
        "memory_ids": sorted(ids), "canonical_id": canonical_id,
        "preference": preference, "verdict": verdict, "material": material,
        "evidence": evidence, "reason": reason, "action": action,
    }


def reverse_audit_unavailable(_error: Exception | None = None) -> ReverseAuditReport:
    """Return the stable fail-open report used when Mnemosyne cannot be queried."""
    return {
        "status": "unavailable", "findings": [], "rehydrate_ids": [],
        "suppressed_ids": [],
        "errors": [{
            "code": "mnemosyne_query_failed",
            "reason": "preference memory query unavailable",
        }],
    }


def reverse_audit(
    memories: list[PreferenceMemory], active_region: list[dict], *, now: str,
    drift_threshold: float = 0.5,
) -> ReverseAuditReport:
    """Audit stored preferences against active post-compaction context.

    The function is deterministic and performs no I/O. ``ABSENT`` live material
    preferences are rehydration candidates; stale, conflicting, malformed, and
    contradicted memories are reported with IDs and never automatically injected.
    """
    now_dt = _parse_timestamp(now)
    if now_dt is None:
        raise ValueError("now must be an ISO-8601 timestamp")
    findings: list[ReverseFinding] = []
    errors: list[dict[str, str]] = []
    live: list[tuple[PreferenceMemory, dict[str, Any]]] = []

    for raw in memories:
        row = raw if isinstance(raw, dict) else {}
        mid = str(row.get("id", "")).strip()
        content = str(row.get("content", "")).strip()
        metadata, metadata_ok = _metadata(row)
        if not mid or not content:
            findings.append(_finding(
                [mid] if mid else [], mid or None, content, "UNKNOWN", False, [],
                "missing id or blank content", "review",
            ))
            errors.append({"code": "malformed_memory", "memory_id": mid,
                           "reason": "missing id or blank content"})
            continue
        if not metadata_ok:
            findings.append(_finding(
                [mid], mid, content, "UNKNOWN", False, [],
                "malformed metadata_json", "review",
            ))
            errors.append({"code": "malformed_memory", "memory_id": mid,
                           "reason": "malformed metadata_json"})
            continue
        superseded_by = str(row.get("superseded_by") or "").strip()
        if superseded_by:
            ml = _memlock_metadata(metadata)
            findings.append(_finding(
                [mid], mid, content, "STORED_STALE",
                bool(ml.get("standing") or ml.get("material")),
                [f"superseded_by={superseded_by}"],
                "stored preference is superseded", "suppress",
            ))
            continue
        valid_until_raw = row.get("valid_until")
        valid_until = _parse_timestamp(valid_until_raw)
        if valid_until_raw and valid_until is None:
            findings.append(_finding(
                [mid], mid, content, "UNKNOWN", False, [],
                "invalid valid_until timestamp", "review",
            ))
            errors.append({"code": "malformed_memory", "memory_id": mid,
                           "reason": "invalid valid_until timestamp"})
            continue
        if valid_until is not None and valid_until <= now_dt:
            ml = _memlock_metadata(metadata)
            findings.append(_finding(
                [mid], mid, content, "STORED_STALE",
                bool(ml.get("standing") or ml.get("material")),
                [], "stored preference expired", "suppress",
            ))
            continue
        live.append((row, metadata))

    grouped: dict[tuple[str, str], list[tuple[PreferenceMemory, dict[str, Any]]]] = {}
    keyed_values: dict[str, set[str]] = {}
    for row, metadata in live:
        ml = _memlock_metadata(metadata)
        key = str(ml.get("preference_key", "")).strip().casefold()
        value = str(ml.get("value", "")).strip().casefold()
        group_key = (key, value) if key and value else ("", str(row["content"]).strip().casefold())
        grouped.setdefault(group_key, []).append((row, metadata))
        if key and value:
            keyed_values.setdefault(key, set()).add(value)

    conflicted_keys = {key for key, values in keyed_values.items() if len(values) > 1}
    handled_conflicts: set[str] = set()
    active_text = "\n".join(_message_text(msg) for msg in active_region)
    user_messages = [msg for msg in active_region if str(msg.get("role", "")).lower() == "user"]

    for group_key in sorted(grouped):
        rows = grouped[group_key]
        key = group_key[0]
        if key in conflicted_keys:
            if key in handled_conflicts:
                continue
            handled_conflicts.add(key)
            conflict_rows = [item for gkey, items in grouped.items() if gkey[0] == key for item in items]
            ids = sorted(str(row["id"]) for row, _ in conflict_rows)
            values = sorted({str(_memlock_metadata(meta).get("value")) for _, meta in conflict_rows})
            findings.append(_finding(
                ids, None, key, "AMBIGUOUS", False, values,
                f"multiple live values stored for preference key {key}", "review",
            ))
            continue

        def rank(item: tuple[PreferenceMemory, dict[str, Any]]) -> tuple[datetime, str]:
            row = item[0]
            stamp = _parse_timestamp(row.get("timestamp")) or _parse_timestamp(row.get("created_at"))
            return stamp or datetime.min.replace(tzinfo=timezone.utc), str(row["id"])

        newest = max(rank(item)[0] for item in rows)
        canonical_candidates = sorted(str(row["id"]) for row, _ in rows if rank((row, {}))[0] == newest)
        canonical_id = canonical_candidates[0]
        canonical_row, canonical_meta = next(item for item in rows if str(item[0]["id"]) == canonical_id)
        ids = sorted(str(row["id"]) for row, _ in rows)
        content = str(canonical_row["content"])
        ml = _memlock_metadata(canonical_meta)
        material = bool(ml.get("standing") or ml.get("material"))
        value = str(ml.get("value", "")).strip()

        contradiction: tuple[str, str] | None = None
        if key and value:
            stored_stamp = rank((canonical_row, canonical_meta))[0]
            prefix = re.compile(rf"\b{re.escape(key)}\s*[:=]\s*([^\n,;.]+)", re.IGNORECASE)
            for msg in user_messages:
                msg_stamp = _parse_timestamp(msg.get("timestamp"))
                newer = msg_stamp is None or msg_stamp > stored_stamp
                if not newer:
                    continue
                msg_text = _message_text(msg).strip()
                # Path 1 — explicit ``key: value`` / ``key=value`` syntax in text.
                match = prefix.search(msg_text)
                if match and match.group(1).strip().casefold() != value.casefold():
                    contradiction = (msg_text, f"active user context sets {key} to {match.group(1).strip()}")
                    break
                # Path 2 — explicit plugin-owned metadata on the active message:
                # metadata.memlock.{preference_key,value} carries the contradiction
                # even when the natural-language text has no key: syntax.
                msg_meta = msg.get("metadata")
                if isinstance(msg_meta, dict):
                    ml_msg = _memlock_metadata(msg_meta)
                    msg_key = str(ml_msg.get("preference_key", "")).strip()
                    msg_value = str(ml_msg.get("value", "")).strip()
                    if (
                        msg_key.casefold() == key.casefold()
                        and msg_value
                        and msg_value.casefold() != value.casefold()
                    ):
                        contradiction = (msg_text, f"active user context sets {msg_key} to {msg_value}")
                        break
        if contradiction:
            findings.append(_finding(
                ids, canonical_id, content, "CONTRADICTED", material,
                [contradiction[0]], contradiction[1], "suppress",
            ))
            continue

        probes_raw = ml.get("probes")
        probes = [str(p) for p in probes_raw if str(p).strip()] if isinstance(probes_raw, list) else _derive_probes(content)
        if not probes:
            findings.append(_finding(
                ids, canonical_id, content, "UNKNOWN", material, [],
                "no deterministic probes available", "review",
            ))
            continue
        hits = _probe_hit(active_text, probes)
        score = hits / len(probes)
        if score >= drift_threshold:
            findings.append(_finding(
                ids, canonical_id, content, "PRESENT", material,
                [p for p in probes if p.casefold() in active_text.casefold()],
                "preference probes present in active context", "none",
            ))
        else:
            findings.append(_finding(
                ids, canonical_id, content, "ABSENT", material, [],
                "no supporting or contradicting evidence in active context",
                "rehydrate" if material else "none",
            ))

    findings.sort(key=lambda f: (f["canonical_id"] or "", f["verdict"], f["memory_ids"]))
    rehydrate_ids = sorted(
        f["canonical_id"] for f in findings
        if f["verdict"] == "ABSENT" and f["material"] and f["canonical_id"]
    )
    suppressed_ids = sorted({
        mid for f in findings
        if f["verdict"] in {"STORED_STALE", "CONTRADICTED", "AMBIGUOUS"}
        for mid in f["memory_ids"]
    })
    return {
        "status": "degraded" if errors else "ok", "findings": findings,
        "rehydrate_ids": rehydrate_ids, "suppressed_ids": suppressed_ids,
        "errors": errors,
    }


def audit_anchors(
    anchors: list[dict],
    active_region: list[dict],
    drift_threshold: float = 0.5,
) -> tuple[list[str], list[str]]:
    """Audit all anchors against the active region of conversation_history.

    Only probes in the ACTIVE region count.  Probes inside the summary
    are demoted by SUMMARY_PREFIX and do not count.

    Returns (alive_ids, drifted_ids).
    """
    # Flatten active region into a single searchable string
    active_text = "\n".join(
        _message_text(msg) for msg in active_region
    ).lower()

    alive: list[str] = []
    drifted: list[str] = []

    for anchor in anchors:
        probes: list[str] = anchor.get("probes", [])
        aid = anchor["id"]
        if not probes:
            # No probes defined — cannot audit.  Treat as alive to avoid
            # false-positive drift.
            alive.append(aid)
            continue
        hits = _probe_hit(active_text, probes)
        score = hits / len(probes)
        if score >= drift_threshold:
            alive.append(aid)
        else:
            drifted.append(aid)

    return alive, drifted


# ── semantic path ────────────────────────────────────────────────────────
# Module-level cache for the sentence-transformers model, keyed by name so a
# config change to embedding_model takes effect.  Tests monkeypatch
# _semantic_model with a stub: any object with .encode(list[str]) ->
# list[vector] works.
_semantic_model: Any = None
_semantic_model_name: str | None = None

_WINDOW_OVERLAP_CHARS = 200
# Hard cap per audit: embedding runs synchronously inside pre_llm_call, so a
# pasted document must not translate into thousands of encode windows.
_MAX_WINDOWS = 256


def _ensure_semantic_model(model_name: str) -> Any | None:
    """Lazy-load sentence-transformers model.  Returns None on failure."""
    global _semantic_model, _semantic_model_name
    if _semantic_model is not None and _semantic_model_name in (None, model_name):
        return _semantic_model
    try:
        from sentence_transformers import SentenceTransformer

        _semantic_model = SentenceTransformer(model_name)
        _semantic_model_name = model_name
        return _semantic_model
    except Exception as exc:
        logger.warning(
            "memlock: semantic model load failed: %s; falling back to keyword",
            exc,
        )
        return None


def _build_windows(active_region: list[dict], window_chars: int) -> list[str]:
    """Split the active region into embedding windows.

    One window per message; messages longer than *window_chars* are split
    into overlapping chunks so an anchor mention spanning a chunk edge is
    still seen whole by at least one window.

    window_chars is clamped above the overlap so the step stays positive,
    and the total window count is capped keeping the most recent windows
    (recency is what matters for "is the instruction still alive").
    """
    window_chars = max(window_chars, _WINDOW_OVERLAP_CHARS + 100)
    windows: list[str] = []
    step = window_chars - _WINDOW_OVERLAP_CHARS
    for msg in active_region:
        text = _message_text(msg).strip()
        if not text:
            continue
        if len(text) <= window_chars:
            windows.append(text)
            continue
        for start in range(0, len(text), step):
            chunk = text[start:start + window_chars]
            if chunk:
                windows.append(chunk)
            if start + window_chars >= len(text):
                break
    return windows[-_MAX_WINDOWS:]


def _cosine(a, b) -> float:
    """Cosine similarity over plain sequences.  Avoids importing
    sentence_transformers.util so stub models need no dependency."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def semantic_audit_anchors(
    anchors: list[dict],
    active_region: list[dict],
    model_name: str = "all-MiniLM-L6-v2",
    sim_threshold: float = 0.65,
    window_chars: int = 1000,
) -> tuple[list[str], list[str]]:
    """Semantic probe: an anchor is alive if its max cosine similarity over
    the active-region windows reaches *sim_threshold*.

    A single whole-region embedding would wash a short instruction out by
    averaging; per-window max similarity is what makes the comparison mean
    anything.  Falls back to keyword audit if the model is unavailable.
    """
    model = _ensure_semantic_model(model_name)
    if model is None:
        logger.info("memlock: semantic unavailable, falling back to keyword")
        return audit_anchors(anchors, active_region)

    try:
        windows = _build_windows(active_region, window_chars)
        if not windows:
            return [], [a["id"] for a in anchors]

        window_embs = model.encode(windows)
        anchor_texts = [a.get("text", "") for a in anchors]
        anchor_embs = model.encode(anchor_texts)

        alive: list[str] = []
        drifted: list[str] = []
        for anchor, emb in zip(anchors, anchor_embs):
            best = max(_cosine(emb, w) for w in window_embs)
            if best >= sim_threshold:
                alive.append(anchor["id"])
            else:
                drifted.append(anchor["id"])
        return alive, drifted
    except Exception as exc:
        logger.warning("memlock: semantic audit failed: %s", exc)
        return audit_anchors(anchors, active_region)
