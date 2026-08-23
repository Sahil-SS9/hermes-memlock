# MemLock v0.5.0 Specification — Portability Spine + Validated Wiki Features

Status: APPROVED scope (Sahil, 2026-08-23, "Option 2")
Baseline: v0.4.0, commit 954c2d6, 110 tests green

## Goals

1. Make MemLock harness-portable: same core runs under Hermes, Claude Code,
   or any MCP-speaking harness.
2. Make provider support "all majors" via one MCP adapter instead of N
   hand-written integrations (memory-provider agnostic constraint holds).
3. Ship the two wiki candidates that survived feasibility review:
   ChronoMem-style pin rollback completion, and ContextNest-style integrity.

## Explicitly OUT (documented, revisit-able)

- SelfCompact pre-compaction preservation: Hermes exposes no compressor hook
  (verified 2026-08-23: hook registry has no pre/post-compaction event).
  Requires upstream contribution first.
- LRE load-bearing scorer: needs outcome-label plumbing that doesn't exist.
- Deployment to ~/.hermes/plugins/memlock: ON HOLD while other agents use it.

## Stage 1 — Core extraction (commit checkpoint)

- New `memlock_core/` package: detection, audit (keyword + semantic),
  store, reminder builder, pin lifecycle, reverse-audit classifier.
- ZERO harness imports in core. Compaction markers become constructor/config
  parameters (`summary_prefixes: list[str]`); defaults keep today's Hermes
  literals. Claude Code passes its compact-boundary marker; MCP callers pass
  theirs per call.
- Existing plugin `__init__.py` becomes a thin shim delegating to core.
- Backward compatibility: existing 110 tests stay green (import-path edits
  acceptable; behaviour changes are not).
- Commit: `refactor: extract harness-agnostic memlock_core`.

## Stage 2 — Harness shims

### Hermes shim
Existing plugin surface, refactored onto core. No behaviour change.

### Claude Code shim (`shims/claude_code/`)
- Hooks mapping: `SessionStart` seeds pins; `PreCompact` snapshots state;
  post-compaction audit+inject rides the hook payload's context mechanism;
  `/guard` equivalent documented as a slash-command skill instruction.
- Ships a hooks registration snippet the setup wizard installs into
  `.claude/settings.json`.
- Tests: fabricated hook JSON payloads through the handler functions.

### MCP server mode (`mcp_server/`)
- Stdio MCP server exposing: `memlock_pin`, `memlock_unpin`,
  `memlock_update`, `memlock_status`, `memlock_audit` (audit returns the
  reminder block for the caller to inject — covers harnesses with no hooks).
- Session identity: caller-supplied `session_id` argument on every tool
  (isolation model identical to the plugin's).
- Tests: in-process client round-trip against the server.

Commit: `feat: claude code shim + mcp server mode`.

## Stage 3 — Provider adapters, MCP-first

- `memlock_adapters/mcp_provider.py`: generic read-only adapter that queries
  an external memory provider's MCP server (config: launch command or URL +
  tool/verb names). One integration covering mem0/OpenMemory, Letta, Zep,
  and anything else speaking MCP.
- Native direct adapters kept as zero-dependency fast paths: `severian.py`
  (PG), `mnemosyne.py` (sqlite). FileStore fallback unchanged.
- Precedence order (documented): explicit host registration > config
  `preference_adapter`. Unknown/failing adapter = warning + disabled.
- Tests: fake MCP server fixture; no real network.

Commit: `feat: mcp-first provider adapter`.

## Stage 4 — Wiki features

### Pin history & rollback (ChronoMem completion)
- `/guard` lists each pinned anchor's version history (id, timestamp, text head).
- `guard_pin` gains `action: "rollback"` with `pin_id`; restores the selected
  prior version from history, pushing the displaced current state forward
  (history grows; nothing is destroyed).
- Durable global pins roll back in the FileStore too.

### Pin-file integrity (ContextNest)
- `persist/manifest.json`: sha256 over every durable pin file, written
  atomically on every change.
- `load_pins()` verifies hashes; mismatched files are quarantined (skipped +
  logged), never silently loaded.
- Session store files gain a lightweight `self_sha256` field checked on load.

Commit: `feat: pin rollback + integrity manifests`.

## Stage 5 — Agent Skills packaging + docs

- Root `SKILL.md` per the agentskills.io open standard (frontmatter
  name/description, usage, setup pointer) so any skills-standard harness can
  install MemLock.
- `memlock_setup.py` extends: `--harness {auto,hermes,claude-code,mcp}`
  detection (Hermes home vs `.claude/` vs neither) wiring the matching shim;
  provider detection unchanged; MCP mode writes the server invocation into
  config.
- README restructure: portability matrix (harness x provider), updated
  quickstart. ARCHITECTURE.md: layer diagram. CHANGELOG: 0.5.0 entry.
- plugin.yaml -> 0.6.0-versioning note: plugin stays 0.4.x-compatible; core
  version tracked in `memlock_core/__init__.py` as 0.5.0.

Commit: `docs: v0.5.0 portability release`.

## Verification gates (every stage)

- Canonical suite command green:
  `uv run --with pytest --python 3.12 --no-project python -m pytest tests/ -q`
- Import smoke with zero optional dependencies installed.
- No named-provider imports anywhere in memlock_core/.
- Atomic writes, fail-open error paths, logger.warning conventions preserved.
- Nothing pushed; deployed plugin directory untouched; no kanban tasks.

## Execution notes for the implementing agent

Work in staged commits exactly as numbered above. If approaching effort
limits, land the current stage cleanly (tests green, committed) and report
honestly what remains rather than leaving the tree dirty.
