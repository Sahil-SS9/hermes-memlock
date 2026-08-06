# PR: Reverse audit — state-to-context verification after compaction

## Blind spot addressed

MemLock's forward audit (`audit_anchors` / `semantic_audit_anchors`) starts
from configured/pinned anchors and asks "did this anchor survive
compaction?". A stored user preference that was never configured as an
anchor is invisible to it. The regression test
`test_forward_only_blind_spot_passes_while_reverse_audit_finds_dependency`
proves the gap: with no anchors configured, forward verification reports
zero drift (`alive_ids=[], drifted_ids=[]`) while `reverse_audit()` finds
the stored material preference `ABSENT` and flags it as a rehydration
candidate.

`reverse_audit()` closes this by starting from **every stored preference**
and asking "is this preference still supported by the active
post-compaction context?".

## What shipped

- `detection.py`: pure, deterministic `reverse_audit(memories, active_region,
  *, now, drift_threshold)` + `reverse_audit_unavailable()` fail-open report.
  Verdicts: `PRESENT / ABSENT / CONTRADICTED / STORED_STALE / AMBIGUOUS /
  UNKNOWN`; structured findings with IDs, evidence, reasons, actions;
  `rehydrate_ids` and `suppressed_ids` lists.
- `__init__.py`: post-compaction reverse pass via a host-supplied preference
  provider (`set_reverse_preference_provider`); `ABSENT + material`
  rehydration candidates merge into the existing priority/slot/char budget;
  provider failure fails open and leaves the turn unchanged.
- `store.py`: optional `reverse` diagnostics on `drift_log` entries.
- `persistence.py`: mnemosyne backend now explicitly raises
  `BackendUnavailableError` instead of silently falling back to FileStore.
- `config.yaml` / `plugin.yaml`: `reverse_audit: false` (disabled by
  default), `reverse_preference_query`, `reverse_limit`; version 0.3.0.
- `docs/REVERSE_AUDIT.md` (new): usage, report interpretation, Mnemosyne
  failure behaviour, forward-vs-reverse distinction, unsupported forms.
  README/ARCHITECTURE/DESIGN updated.
- Tests: `tests/test_reverse_audit_unit.py` (implementation unit tests,
  incl. pre_llm integration + provider-failure), `tests/test_reverse_audit.py`
  + `tests/fixtures/reverse_audit_stale.json` (STALE benchmark suite).

## Contract reconciliation (from t_87076251's 3-pass/5-mismatch report)

1. **Contradiction semantics** — impl now honours
   `metadata.memlock.{preference_key,value}` on active user messages
   (contract §5), not only `key: value` text syntax. Fixture case
   `contradicted-current-context` now returns `CONTRADICTED/suppress`.
2. **Superseded wording** — reason unified to `"stored preference is
   superseded"`, evidence `["superseded_by=<id>"]` (contract §94);
   `STORED_STALE` findings preserve explicit materiality.
3. **ABSENT wording** — unified to the contract-stable
   `"no supporting or contradicting evidence in active context"`.
4. **Error key** — query-failure errors use `reason` consistently across
   the pure function, the boundary harness and the fixture (was split
   `reason`/`message`).
5. **Materiality gap (impl gap flagged)** — kept per contract §100:
   `material` is true only with explicit `standing`/`material` metadata;
   non-tagged stored preferences are reported but never rehydrated. This
   is documented as an intentionally unsupported form.

## Test evidence

- Full suite: **74 passed** (0.13s) — baseline 53 (52 original + 1 new
  drift-log test) + 21 reverse-audit tests.
- STALE benchmark suite: 8/8 cases pass, including the blind-spot
  regression.
- `python3 -m py_compile detection.py __init__.py store.py persistence.py` — OK.
- No unrelated repository changes: diff vs `f6cfb8b` is exactly 15 files
  (4 source, 2 config, 3 docs, 6 test files).

## Intentionally unsupported preference forms

- Semantic-only contradiction (meaning-based, no explicit key/value or
  metadata) → classified `ABSENT`, never `CONTRADICTED` (no LLM judgement).
- Preferences without explicit probes and no derivable keywords → `UNKNOWN`.
- Non-material preferences (no `standing`/`material` metadata) → never
  rehydrated.
- Automatic supersession from recency → never inferred; conflicting live
  values are `AMBIGUOUS`.
- Provider writes — reverse audit never writes/invalidates Mnemosyne.

## Files changed (15)

```
README.md, __init__.py, config.yaml, detection.py, persistence.py,
plugin.yaml, store.py, docs/ARCHITECTURE.md, docs/DESIGN.md,
docs/REVERSE_AUDIT.md (new), tests/fixtures/reverse_audit_stale.json,
tests/test_lifecycle.py, tests/test_persistence.py,
tests/test_reverse_audit.py, tests/test_reverse_audit_unit.py (new)
```

Commit: `7e1b61a` on branch `task/t_dd7a340f` (parent `f6cadff` benchmarks,
grandparent `f6cfb8b` main).
