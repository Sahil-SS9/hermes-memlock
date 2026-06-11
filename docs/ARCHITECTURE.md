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

### Optional dispatch patch

Vanilla Hermes does not forward `session_id` to plugin tool handlers.
Without this, `guard_pin` binds to the **last-seen session**: correct in
single-session environments but a race under concurrent gateway sessions.

See `docs/optional-dispatch-patch.md` for the 3-line fix.
