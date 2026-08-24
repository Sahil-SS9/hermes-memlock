# Changelog

## 0.5.0 — Stage 5: Agent Skills packaging + docs (complete)

### Agent Skills packaging (SKILL.md)
- Added root SKILL.md per agentskills.io open standard with YAML frontmatter
- Includes description, quickstart per harness, guard_pin usage examples, and status checking

### Setup wizard harness detection (--harness)
- Extended memlock_setup.py with --harness {auto,hermes,claude-code,mcp} 
- Auto-detection: HERMES_HOME layout => hermes; .claude/settings.json => claude-code; neither => mcp
- hermes: existing behavior unchanged (writes to config.yaml)
- claude-code: installs hooks via shims.claude_code.settings_installer.apply_to_file
- mcp: prints MCP config snippet for user's MCP client config
- Provider detection (severian/mnemosyne/mcp/none) stays as-is and composes with harness selection

### Documentation updates
- README.md: Added portability matrix table (Hermes/Claude Code/MCP-generic vs hooks support/injection mechanism/provider adapters)
- README.md: Updated quickstart to reference SKILL.md
- docs/ARCHITECTURE.md: Added layer diagram section (core -> shims -> adapters)
- CHANGELOG.md: This 0.5.0 entry summarizing all five stages
- memlock_core/__init__.py: Bumped __version__ to '0.5.0'

### Test extensions
- Extended tests/test_setup.py with >=4 new test functions:
  - --harness auto-detection matrix (hermes-home present, .claude present, neither)
  - claude-code apply path creating hooks in temp settings.json
  - mcp mode printing snippet without writing anywhere unexpected

## 0.4.0 — provider-agnostic adapters, setup wizard, pin update-in-place
 — provider-agnostic adapters, setup wizard, pin update-in-place

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
