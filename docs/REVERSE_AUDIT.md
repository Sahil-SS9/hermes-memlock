# Reverse audit — developer guide

`reverse_audit()` is the state-to-context complement of MemLock's existing
forward audit. It is a pure, deterministic function in `detection.py`: it
takes the stored preference memories that survived compaction and checks
whether the active (post-compaction) context still preserves, contradicts,
or provides no evidence for each one.

This document is for developers wiring MemLock into a host or reading its
reports. It does not describe user-facing pin behaviour.

## When to use it

- After a compaction event (`SUMMARY_PREFIX` detected) or on the safety net,
  inside `_on_pre_llm()`, immediately after `split_context()`.
- Whenever you need to know which stored user preferences were *not* carried
  into the active context, not just which configured anchors drifted.
- The function itself performs **no I/O**. Retrieval of the preference rows
  happens in the host adapter (`_reverse_preference_provider`); `reverse_audit()`
  only classifies rows you hand it.

## Usage

```python
from detection import reverse_audit

report = reverse_audit(
    memories,          # list[PreferenceMemory] — complete candidate rows
    active_region,     # split_context() output; user_message appended if absent
    now="2026-08-04T08:00:00Z",  # required ISO-8601; deterministic expiry
    drift_threshold=0.5,         # probe hit fraction for PRESENT
)
```

In the plugin the adapter is registered once:

```python
import memlock
memlock.set_reverse_preference_provider(provider)  # provider(query, limit) -> list[dict]
```

The adapter returns complete preference rows (with lifecycle fields). When
`reverse_audit: true` in `config.yaml` and a provider is registered, the
reverse pass runs after each compaction and its `rehydrate_ids` merge into
the same priority/slot/char budget path as drifted anchors.

## Report shape and interpretation

```python
{
  "status": "ok" | "degraded" | "unavailable",
  "findings": [
    {
      "memory_ids": ["dup-new", "dup-old"],   # all rows grouped into this finding
      "canonical_id": "dup-new",              # deterministic newest row
      "preference": "Use short paragraphs",   # canonical content
      "verdict": "PRESENT" | "ABSENT" | "CONTRADICTED"
               | "STORED_STALE" | "AMBIGUOUS" | "UNKNOWN",
      "material": True | False,               # explicit standing/material marking
      "evidence": ["short paragraphs"],       # matched probe / contradicting text
      "reason": "preference probes present in active context",
      "action": "none" | "rehydrate" | "suppress" | "review",
    },
  ],
  "rehydrate_ids": ["pref-concise"],   # ABSENT + material → rehydration candidates
  "suppressed_ids": ["superseded"],    # STORED_STALE / CONTRADICTED / AMBIGUOUS
  "errors": [{"code": "malformed_memory", "memory_id": "...", "reason": "..."}],
}
```

### Verdict semantics

| Verdict | Meaning | Action |
|---|---|---|
| `PRESENT` | Probes meet `drift_threshold` in active context | `none` |
| `ABSENT` | No support and no explicit contradiction | `rehydrate` only when `material` |
| `CONTRADICTED` | Active context explicitly conflicts with the stored value | `suppress` |
| `STORED_STALE` | Expired or superseded; not a live preference | `suppress` |
| `AMBIGUOUS` | Multiple live values for one key; no deterministic winner | `review` |
| `UNKNOWN` | Malformed row / no deterministic comparison possible | `review` |

`rehydrate_ids` lists only `ABSENT + material` canonical IDs. Non-material
absent preferences are reported but never rehydrated (contract §100: without
explicit `standing`/`material` metadata, default is non-material).

`suppressed_ids` unions every memory id from `STORED_STALE`, `CONTRADICTED`
and `AMBIGUOUS` findings — those rows are excluded from automatic reinjection.

`status` is `degraded` when any malformed row was encountered (valid rows
are still audited), `unavailable` when retrieval failed, `ok` otherwise.

## Forward vs reverse — the blind spot

- **Forward audit** (`audit_anchors` / `semantic_audit_anchors`) starts from
  the configured/pinned anchors and asks "did this anchor's probes survive?"
  A preference that was never configured as an anchor is **invisible** to it.
- **Reverse audit** starts from *every stored preference* and asks "is this
  preference still supported by active context?"

The blind spot regression (`test_forward_only_blind_spot_passes_while_reverse_audit_finds_dependency`)
proves the case: with no anchors configured, `audit_anchors([], active_region)`
returns no drift (`alive_ids=[]`, `drifted_ids=[]`) while `reverse_audit()`
correctly reports the stored material preference as `ABSENT` and rehydration
candidate. Forward-only verification passes; the dependency is only visible
from the reverse pass.

## Mnemosyne failure behaviour

Retrieval happens outside `reverse_audit()`. When the adapter raises, times
out, or returns a non-list, the plugin logs once and returns the stable
fail-open report:

```python
{
  "status": "unavailable",
  "findings": [],
  "rehydrate_ids": [],
  "suppressed_ids": [],
  "errors": [{"code": "mnemosyne_query_failed", "reason": "preference memory query unavailable"}],
}
```

- The Hermes turn continues **unchanged** (no rehydration, no injection).
- No findings are reported — an unavailable query must not be misread as
  "no stale preferences".
- The error uses the `reason` key (not `message`) consistently across the
  pure function and the boundary harness.
- Malformed individual rows degrade the report to `status="degraded"` but do
  not hide valid findings; malformed rows become `UNKNOWN`/`review`, never
  `ABSENT`.

## Intentionally unsupported preference forms

- **Semantic contradiction.** A stored preference contradicted only by
  *meaning* (not by explicit `key: value` / `key=value` text or by
  `metadata.memlock.{preference_key,value}` on an active user message) is
  classified `ABSENT`, not `CONTRADICTED`. No LLM judgement is used.
- **No explicit probes.** Memories without `metadata_json.memlock.probes`
  fall back to deterministic keyword derivation; preferences with no
  derivable probes become `UNKNOWN`/`review`.
- **Non-material preferences.** Biographical or context-only preferences
  without explicit `standing`/`material` metadata are never rehydrated.
- **Automatic supersession.** Recency never invents supersession when
  Mnemosyne has not recorded it; conflicting live values are `AMBIGUOUS`.
- **Provider writes.** The reverse pass never writes, invalidates, or
  updates Mnemosyne. Lifecycle changes are a separate reviewed workflow.
