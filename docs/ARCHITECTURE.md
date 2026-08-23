# MemLock Architecture

## Compaction detection and anchor rehydration

### Flowchart

```mermaid
flowchart TD
    A[pre_llm_call hook fires] --> D[Increment turn counter]
    D --> B[SCAN conversation_history for SUMMARY_PREFIX]
    B --> C{New compaction?<br>prefix found + SHA256 hash changed}
    C -->|No| E{Turn >= 40 since last reinjection?}
    E -->|No| F[Return None, nothing to do]
    E -->|Yes| G[Safety-net: reinject anchors within budgets]
    G --> R
    C -->|Yes| J[Record new compaction event]
    J --> K[Split context into summary vs active regions]
    K --> L[Audit all anchors against active region only]
    L --> M{Threshold fraction of probes<br>found in active region?}
    M -->|Yes| N[Mark all alive, compute integrity score]
    N --> F
    M -->|No| O[Mark drifted, compute integrity score]
    O --> P{Score < alert_floor?}
    P -->|Yes| Q[Log warning with drifted ids]
    Q --> R[Select casualties by priority (max_slots × max_chars)]
    P -->|No| R
    R --> S[Build reminder block]
    S --> T[Return context dict → appended to user turn]

    K -.->|if reverse_audit enabled| R1[Query preference provider]
    R1 --> R2[reverse_audit: classify each stored preference]
    R2 --> R3[merge ABSENT+material rehydrate_ids into casualty selection]
    R3 --> R
```

### Storage layout

Each session gets one JSON file:

```
~/.hermes/memlock/
├── session-abc123.json
└── session-def456.json
```

Per-session state:

```json
{
  "session_id": "abc123",
  "anchors": {
    "pin_1689345678_0": {
      "id": "pin_1689345678_0",
      "text": "Always reply in bullet points",
      "reminder": "Always reply in bullet points",
      "priority": 80,
      "probes": ["bullet", "points", "reply"],
      "pinned": true,
      "drifted": false,
      "last_alive_turn": 42
    }
  },
  "static_anchor_ids": [],
  "pinned_count": 1,
  "last_summary_hash": "a1b2c3...",
  "last_compaction_at": 1689345678.0,
  "integrity_score": 100,
  "last_reinject_turn": 42,
  "drift_log": [...],
  "last_alert_at": null
}
```

### Key design decisions

1. **Active-region-only audit.** Probes hitting only inside the summary
   block do NOT count as survival: the SUMMARY_PREFIX demotes that text
   to background reference. This is the core behavioural insight.
2. **Session isolation.** Each Hermes gateway session gets its own store
   file. No cross-session anchor leakage.
3. **Priority-descending rehydration.** When slot pressure forces selection,
   higher-priority anchors are reinjected first. Same-priority tiebreaks
   are alphabetical by id.
4. **Injection modes.** `on-drift` audits and reinjects casualties only;
   `always` injects the reminder block every turn. Both respect the slot
   and char budgets in `_select_casualties`.
5. **Safety net.** Every 40 turns without a new compaction (an unchanged
   summary hash counts as no new compaction), anchors are reinjected
   regardless of drift status (anti-entropy), cut by the same `max_slots`
   and `max_reminder_chars` budgets.
6. **Alert cooldown.** Integrity alerts are rate-limited to one per 1800s
   to prevent log noise when the model is mid-task.
7. **Reverse audit (state-to-context).** After a compaction, stored
   preferences are also queried (when configured with a host adapter) and
   classified by `reverse_audit()`. `ABSENT + material` preferences merge
   into the same rehydration budgets; stale/contradicted/ambiguous rows are
   excluded and never auto-reinjected. This closes the forward-only blind
   spot where preferences that were never configured as anchors were
   invisible to the audit. See `docs/REVERSE_AUDIT.md`.
8. **Update-in-place with bounded history.** `guard_pin(pin_id, text)`
   rewrites an existing anchor without changing its id, so drift state,
   scores and references stay continuous across the edit. The pre-update
   version is appended to a per-anchor `history` list capped at
   `HISTORY_CAP = 5` entries; global-scoped pins re-persist through the
   durable store (`save_pin` upserts by id), so future sessions seed the
   updated wording.

### Adapter layer (preference providers)

The reverse audit needs *some* source of stored preferences, but the plugin
core must never import a memory provider. That boundary lives in
`memlock_adapters/`, the only package allowed to know provider specifics:

- **Config-selected, lazily loaded.** `preference_adapter: severian |
  mnemosyne` names an entry in the registry; the factory runs at the first
  reverse-audit call (module-level cache), never at import time, so a
  missing driver can't break plugin load or the `pre_llm_call` hook.
- **Explicit registration wins.** A host-registered callback via
  `set_reverse_preference_provider(fn)` takes precedence over any config
  selection for the lifetime of the process (host adapters predate the
  registry and must keep working).
- **Fail-open everywhere.** Missing driver, unreachable DB, malformed rows:
  each degrades to one warning + empty list / skipped row. An unknown
  adapter name resolves to None = reverse pass disabled — never an error.
- **Read-only by construction.** Severian queries `records` over psycopg
  (DSN: config `adapter_dsn` → `SEVERIAN_DSN`) mapping JSONB payload keys;
  Mnemosyne opens its sqlite file `mode=ro` (path: config `adapter_db_path`
  → `MNEMOSYNE_DB_PATH`/`MNEMOSYNE_DATA_DIR` → `$HERMES_HOME/mnemosyne/
  data/mnemosyne.db`) and maps `working_memory` rows via
  `cursor.description` so column drift across Mnemosyne versions degrades
  to None fields instead of KeyErrors.
- **Normalisation contract.** Every row passes `normalise_row()`: it needs
  a usable `id` AND `content`, fills missing optional columns with None,
  coerces datetimes to ISO strings, and returns None for non-dict junk so
  the finding/memory join stays deterministic.

### Setup wizard

`memlock_setup.py` (stdlib-only) detects which provider backs this install
(plugin dirs, `plugins.enabled`, env vars, DB file) and writes only the
`memlock:` section of `$HERMES_HOME/config.yaml` — timestamped backup
first, PyYAML required for any write (without it: printed manual steps),
detection verdicts reported honestly even when overridden by `--provider`.
Pruning (`--prune-days N`, N ≥ 7) deletes old session stores by mtime and
never touches `memlock/persist/`. Exit codes: 0 ok, 1 nothing detected,
2 error.

### Optional dispatch patch

Vanilla Hermes does not forward `session_id` to plugin tool handlers.
Without this, `guard_pin` binds to the **last-seen session**: correct in
single-session environments but a race under concurrent gateway sessions.

See `docs/optional-dispatch-patch.md` for the 3-line fix.
