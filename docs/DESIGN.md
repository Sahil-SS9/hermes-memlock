# MemLock Design Document

## Problem

Hermes Agent's context compressor wraps compacted turns in a `SUMMARY_PREFIX`
marker. The model treats everything below this marker as background reference,
not active instructions. Standing instructions (output format, tool preference,
writing style, brand voice) that were set before compaction drift out of the
model's working memory after compaction fires.

Community users have reported this as "the agent forgot my instruction": the
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
   instruction format; it reads the reminder as a natural language prompt.
2. **Deterministic detection.** Compaction is detected by scanning for the
   exact `SUMMARY_PREFIX` string from `agent.context_compressor`. No LLM
   judgement, no probability thresholds.
3. **Session-isolated state.** Each Hermes agent session gets its own JSON
   store file. No cross-session drift.
4. **Fail-open.** The Hermes hook runner swallows hook exceptions, so a
   failure in MemLock means the turn continues without rehydration. Pins
   survive in the store for the next turn.
5. **Bounded state.** Drift log is capped at 20 events and pins at
   `max_pins` per session. Store files are created on demand; they are
   small JSON files and persist until removed by the operator.
6. **Two anchor sources, one audit.** Static anchors come from config and
   reseed on every session start; dynamic pins come from `guard_pin`.
   Both go through the identical probe audit and rehydration path.

## Probe-based detection rationale

Keyword probes are used instead of embeddings by default because:

1. No additional model dependency (no sentence-transformers install).
2. Deterministic and testable: no embedding drift, no model version churn.
3. Substring matching catches the typical compaction failure mode: the
   instruction is physically present in context but in the wrong region.
4. Semantic mode (windowed embeddings, max cosine over windows) is available
   for users who install sentence-transformers; it falls back to keyword
   probes when the model is unavailable.

## Injection modes

`on-drift` (default) treats reinjection as a repair action: audit on
compaction, reinject only the casualties. `always` injects the reminder
block every turn for sessions where deterministic presence matters more
than token cost and prompt-cache stability. The audit still runs on
compactions in both modes so the integrity score stays honest.

## Core insight

The SUMMARY_PREFIX finding, that Hermes deliberately demotes compacted
content to background reference, is the design discovery that makes this
plugin necessary. Without understanding this, "but the instruction is still
in the context!" is a baffling user experience. With it, the fix is obvious:
don't let critical instructions drift into the summary region.
