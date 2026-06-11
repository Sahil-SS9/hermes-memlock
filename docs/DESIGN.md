# MemLock Design Document

## Problem

Hermes Agent's context compressor wraps compacted turns in a `SUMMARY_PREFIX`
marker. The model treats everything below this marker as background reference,
not active instructions. Standing instructions (output format, tool preference,
writing style, brand voice) that were set before compaction drift out of the
model's working memory after compaction fires.

Community users have reported this as "the agent forgot my instruction" — the
instruction is still in the context window, but the model treats the summary
region as demoted reference material.

## Solution

Hook into the agent's own turn lifecycle (`pre_llm_call`), detect compaction
events by scanning for the `SUMMARY_PREFIX` literal, hash the summary body
to detect *new* compactions (one compaction can produce multiple turns),
audit pinned anchors against the active (non-summary) region only, and
rehydrate drifted anchors as a structured reminder block.

## Design constraints

1. **Zero model behaviour change.** The reminder block is appended to the
   user turn before the model sees it. The model never needs to learn a new
   instruction format — it reads the reminder as a natural language prompt.
2. **Deterministic detection.** Compaction is detected by scanning for the
   exact `SUMMARY_PREFIX` string from `agent.context_compressor`. No LLM
   judgement, no probability thresholds.
3. **Session-isolated state.** Each Hermes agent session gets its own JSON
   store file. No cross-session drift.
4. **Fail-open.** If the `pre_llm_call` hook throws an exception, the turn
   continues without rehydration. Pins survive in the store for the next turn.
5. **Self-cleaning.** Drift log is capped at 20 events. Store files are
   created on demand and cleaned when the session expires.
6. **Two-tier anchor protection.** Static anchors (from config) survive
   every compaction without needing `guard_pin`. Dynamic pins (from tool
   calls) use probe-based drift detection.

## Probe-based detection rationale

Keyword probes are used instead of embeddings by default because:

1. No additional model dependency (no sentence-transformers install).
2. Deterministic and testable — no embedding drift, no model version churn.
3. Substring matching catches the typical compaction failure mode: the
   instruction is physically present in context but in the wrong region.
4. Semantic mode exists as a P3 upgrade path for users who want it.

## Core insight

The SUMMARY_PREFIX finding — that Hermes deliberately demotes compacted
content to background reference — is the design discovery that makes this
plugin necessary. Without understanding this, "but the instruction is still
in the context!" is a baffling user experience. With it, the fix is obvious:
don't let critical instructions drift into the summary region.
