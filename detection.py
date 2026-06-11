"""Compaction detection and keyword probe audit for MemLock.

Compaction: scan conversation_history for the compressor's SUMMARY_PREFIX
literal.  Hash the summary body; new/changed hash = compaction event.

Keyword probe: scope to the non-summary region only.  An anchor's probes
hitting only inside the summary block are NOT survival — the SUMMARY_PREFIX
demotes that text to background.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

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
