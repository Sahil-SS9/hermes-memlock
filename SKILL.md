--- 
name: memlock
description: |
  MemLock protects AI agent memory from corruption and drift by detecting and
  reversing unwanted compaction, summarization, or loss of important context.
  It works by pinning key memories (facts, decisions, preferences) and auditing
  the memory store for changes to those pins. When drift is detected, MemLock
  can restore the pinned content from history or inject reminders to reorient
  the agent.

  Use MemLock when you need long-term memory stability across sessions, especially
  for agents that perform iterative reasoning, learning, or decision-making where
  preserving specific facts is critical. It is harness-agnostic: the same core
  runs under Hermes, Claude Code, or any MCP-speaking harness, and supports
  multiple memory backends (Severian, Mnemosyne, or any MCP-compatible provider)
  via a single adapter.

Quickstart:
  Install via your harness:
  - Hermes: `hermes plugin install memlock` (or copy this repo to ~/.hermes/plugins/memlock)
  - Claude Code: Run `python3 memlock_setup.py --harness claude-code` to install hooks
    into .claude/settings.json, then ensure the memlock MCP server is available.
  - Generic MCP: Run `python3 -m mcp_server` and configure your MCP client to
    connect to it using stdio (command: `python3 -m mcp_server`).

  After installation, the model can call the `guard_pin` tool. Real parameters
  (see its schema): `text`, `pin_id`, `unpin`, `reminder`, `priority`,
  `probes`, `scope` ("session"|"global"), `action` ("rollback").
  - Pin: guard_pin { text: "Always reply in bullet points", priority: 80 }
  - Update in place (keeps id, archives old version to history):
    guard_pin { pin_id: "pin_1a2b3c4d", text: "new wording" }
  - Roll back to a prior version from history:
    guard_pin { action: "rollback", pin_id: "pin_1a2b3c4d", version: -2 }
  - Unpin: guard_pin { unpin: "pin_1a2b3c4d" } (add scope: "global" for durable pins)
  - Status/audit: `/guard` (Hermes) or the memlock_status MCP tool

  MemLock works without a dedicated memory provider; keyword/semantic audits
  function standalone. For preference-aware audits, install Severian or Mnemosyne,
  or connect any MCP-compatible memory provider.

---
MemLock is a memory integrity system for AI agents that prevents silent corruption
of important memories (pins) due to compaction, summarization, or context drift.
It operates by:
1. Allowing users to pin specific memories (facts, decisions, preferences) via
   `guard_pin` or equivalent harness-specific mechanisms.
2. Periodically auditing the memory store for changes to pinned content using
   keyword and semantic checks.
3. On detecting drift, providing rollback to prior pinned versions or injecting
   reminder reminders to reorient the agent.

MemLock is harness-portable: the same core logic runs under Hermes (as a plugin),
Claude Code (via hooks installed by `memlock_setup.py`), or any MCP-speaking
harness (via the MemLock MCP server). Provider adapters are MCP-first: a single
generic MCP adapter connects to any memory provider speaking MCP, with fast-path
direct adapters for Severian (PostgreSQL) and Mnemosyne (SQLite).

Key features:
- **Pin lifecycle**: create, list, update, rollback pins with full version history.
- **Audit modes**: keyword (exact string) and semantic (embedding-based) drift detection.
- **Integrity manifests**: SHA-256 hashes prevent silent corruption of pin files.
- **Provider agnosticism**: works standalone or with Severian, Mnemosyne, or any
  MCP-compatible memory provider (via the memlock_adapters/mcp_provider.py adapter).
- **Zero hard dependencies**: core uses only stdlib; optional adapters require
  their respective databases but are detected automatically.

When to use:
- Long-running agent sessions where memory drift could corrupt reasoning.
- Any setting where specific facts, decisions, or preferences must be preserved
  across compaction or summarization events.
- Multi-agent systems needing shared memory integrity guarantees.
- Environments where memory providers may be swapped or upgraded without losing
  pin history.

Avoid using MemLock if:
- You require sub-millisecond memory access latency (audits add overhead).
- Your memory provider cannot be queried for audit (though standalone keyword
  audits still work).
- You need real-time blocking of memory writes (MemLock is detective, not
  preventive—it detects and corrects drift after the fact).