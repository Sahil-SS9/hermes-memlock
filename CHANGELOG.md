# Changelog

## 0.5.0 — Stage 4: pin rollback + integrity manifests (in progress)

### Pin version history & rollback (ChronoMem completion)

- `/guard` (and `memlock_status`) lists each pinned anchor's version history
  compactly: pin id, prior-version count, current text head, plus the oldest→
  newest history text heads. Pins with no edits add no noise.
- `guard_pin` gains `action: "rollback"` with a required `pin_id`; restores a
  selected prior version from that anchor's history. Selection semantics:
  default (`version` omitted or -1) = most recent previous version;
  `version: 0` = oldest retained; any integer index works; out-of-range is a
  clear error leaving state untouched.
- Rollback destroys nothing: the displaced CURRENT state is pushed onto the
  history first, so rollback itself remains rollback-able; repeated default
  rollbacks swap between the two most recent states (classic undo), and the
  HISTORY_CAP=5 trim applies exactly as for updates.
- Global-scoped pins re-persist to the durable FileStore on rollback so
  future sessions seed the restored wording ("global copy rolled back").
- Works on every surface: Hermes shim handler, `MemlockService.rollback_pin`,
  and MCP `memlock_update` with `action: "rollback"` (only `session_id` +
  `pin_id` required there).

### Pin-file integrity manifests (ContextNest)

- Durable pins: `persist/manifest.json`
  (`{version: 1, generated_at, pins: {<id>: {sha256, size}}}`) is rewritten
  atomically on EVERY durable-store change — `save_pin`, `remove_pin` and
  rollback re-persists included.
- `FileStore.load_pins()` verifies each pin file against the manifest;
  mismatched files are quarantined (skipped + one warning naming them),
  never silently loaded. A missing manifest bootstraps a fresh baseline
  from the current files (pre-0.5.0 installs keep working); an unparseable
  manifest warns and rebuilds the baseline instead of failing.
- Session store JSON gains `self_sha256` (sha256 of the anchors payload)
  written on every save. On load a mismatch logs a warning and proceeds —
  integrity is advisory for session stores, enforcing for durable pins.
  Files from older versions without the field load silently.

### Tests

- New suites: `tests/test_rollback.py` (restoration under same id,
  displaced-state preservation, cap across repeated rollbacks, durable
  re-sync, unknown-pin errors, version-index selection, /guard history
  display, MCP surface, post-compaction rehydration of restored pins) and
  `tests/test_integrity.py` (manifest write/refresh/quarantine/bootstrap/
  corrupt-manifest fail-open, verify opt-out, session-store hash round-trip,
  mismatch warning, legacy-file compatibility). Suite grows to 195 tests.

## 0.4.0 — provider-agnostic adapters, setup wizard, pin update-in-place

### Preference adapter layer (`memlock_adapters/`)

- New `memlock_adapters` package: the only place that knows memory-provider
  specifics. The plugin core stays provider-agnostic.
- `severian` adapter: read-only PostgreSQL query against Severian's
  `records` table via psycopg/psycopg2 (imported lazily at query time).
  DSN resolution: config `adapter_dsn` → `SEVERIAN_DSN`. Maps JSONB payload
  keys onto the reverse-audit row shape; missing payload keys are absent,
  never errors.
- `mnemosyne` adapter: read-only sqlite query against Mnemosyne's
  `working_memory` table, stdlib-only, connection opened `mode=ro`. Path
  resolution: config `adapter_db_path` → `MNEMOSYNE_DB_PATH` /
  `MNEMOSYNE_DATA_DIR` → `$HERMES_HOME/mnemosyne/data/mnemosyne.db`.
  Columns resolved defensively via `cursor.description` so schema drift
  across Mnemosyne versions degrades to None fields.
- Fail-open contract everywhere: missing driver, unreachable DB and
  malformed rows each produce one warning plus an empty list or skipped
  row — a broken provider can never take down the `pre_llm_call` hook.
  Unknown adapter names disable the reverse pass with a warning.
- `normalise_row()` normalises every provider row (requires usable id +
  content, fills optional columns with None, coerces datetimes to ISO).
- Config selection is lazy: `preference_adapter: severian | mnemosyne`
  resolves at the first audit, not at import. An explicitly registered host
  callback (`set_reverse_preference_provider`) still takes precedence.

### Setup wizard (`memlock_setup.py`)

- Stdlib-only CLI that detects the backing memory provider (Severian plugin
  dir / `SEVERIAN_DSN`, Mnemosyne plugin dir / `plugins.enabled` entry /
  database file) and writes only the `memlock:` section of
  `$HERMES_HOME/config.yaml`.
- Timestamped backup before any write; without PyYAML it prints manual
  instructions instead of risking a mangled config.
- Detection verdicts reported honestly even when overridden by
  `--provider`; Severian wins over Mnemosyne when both are present.
- `--dry-run` prints the report and planned keys, writes nothing.
- `--prune-days N` (minimum 7) deletes old session store files by mtime;
  never touches `memlock/persist/` or non-`.json` files.
- Exit codes: 0 ok, 1 nothing detected (guidance printed), 2 error.

### Pin update-in-place (`guard_pin` with `pin_id`)

- `guard_pin(pin_id=..., text=...)` updates an existing pin in place: same
  anchor id retained, text/reminder/priority/probes rewritten with the same
  whitespace-flattening and clamping rules as a fresh pin.
- Previous versions kept per anchor in a bounded history list (last 5),
  visible in the store for `/guard`-style introspection.
- Missing ids return a clear error listing available pin ids.
- Global-scoped pins re-persist to the durable store on update (upsert by
  id) so future sessions seed the updated wording; session-scoped updates
  never create durable copies.

### Tests

- New dedicated suites: `tests/test_adapters.py` (fabricated sqlite DBs +
  monkeypatched psycopg factories — no real network/DB),
  `tests/test_setup.py` (detection matrix, backup scoping, merge
  preservation, dry run, prune foot-guards, exit codes, PyYAML-less
  refusal path) and `tests/test_update_pin.py` (id stability, history cap,
  durable re-sync, end-to-end rehydration after update). Suite grows from
  78 to 110 tests.
