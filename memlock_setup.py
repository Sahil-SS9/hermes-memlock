#!/usr/bin/env python3
"""MemLock setup/detection wizard — stdlib-only.

Detects which memory provider backs this Hermes installation and writes the
correct ``memlock:`` section into ``$HERMES_HOME/config.yaml``, so the
reverse-audit preference adapter matches reality instead of guessing.

Design constraints mirror the plugin itself:
  - memory-provider agnostic: detection REPORTS what it finds; it never
    imports a provider package.
  - fail-safe on config writes: back up first, mutate only the ``memlock:``
    section, and if no YAML parser is available, print manual instructions
    rather than risk mangling the user's config.
  - pruning is conservative by construction (N >= 7, persist/ excluded,
    session stores matched by exact filename shape).

Exit codes: 0 ok, 1 nothing detected (manual guidance printed), 2 error.

Usage:
  python memlock_setup.py                     # auto-detect, apply
  python memlock_setup.py --dry-run           # report only, change nothing
  python memlock_setup.py --provider severian # force a detection verdict
  python memlock_setup.py --prune-days 30     # delete old session stores
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

PROVIDERS = ("auto", "severian", "mnemosyne", "none")
HARNESSES = ("auto", "hermes", "claude-code", "mcp")

_MEMLOCK_SECTION_RE = re.compile(r"(?ms)^memlock:\s*\n(?! )")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()


# ── detection ────────────────────────────────────────────────────────────


def detect_provider(home: Path) -> tuple[str, list[str]]:
    """Return (verdict, findings). All findings are reported honestly.

    Severian wins over Mnemosyne when both are present because the reverse
    audit prefers canonical observations over working-memory rows; the
    report lists everything seen either way.
    """
    findings: list[str] = []

    sev_dir = home / "plugins" / "severian"
    sev_plugin = (sev_dir / "plugin.yaml").exists()
    if sev_plugin:
        findings.append(f"found Severian plugin dir: {sev_dir}")
    else:
        findings.append(f"no Severian plugin dir at {sev_dir}")

    sev_dsn_env = os.environ.get("SEVERIAN_DSN", "").strip()
    if sev_dsn_env:
        findings.append("found SEVERIAN_DSN environment variable (PostgreSQL backend)")
    else:
        findings.append("no SEVERIAN_DSN in environment")
    severian_detected = bool(sev_plugin or sev_dsn_env)

    mnem_dir = home / "plugins" / "mnemosyne"
    mnem_plugin = mnem_dir.exists()
    if mnem_plugin:
        findings.append(f"found Mnemosyne plugin dir: {mnem_dir}")
    else:
        findings.append(f"no Mnemosyne plugin dir at {mnem_dir}")

    mnem_enabled = _mnemosyne_in_enabled_config(home)
    if mnem_enabled:
        findings.append("mnemosyne listed in config.yaml plugins.enabled")
    else:
        findings.append("mnemosyne not listed in config.yaml plugins.enabled")

    mnem_db = home / "mnemosyne" / "data" / "mnemosyne.db"
    if mnem_db.exists():
        findings.append(f"found Mnemosyne database file: {mnem_db}")
    else:
        findings.append(f"no Mnemosyne database at {mnem_db}")

    mnemosyne_detected = bool(mnem_plugin or mnem_enabled)

    if severian_detected:
        return "severian", findings
    if mnemosyne_detected:
        return "mnemosyne", findings
    return "none", findings


def _read_yaml_or_none(path: Path) -> Any:
    """Parse YAML with PyYAML when available, else a minimal block scan."""
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    except ImportError:
        return _shallow_enabled_scan(text)
    except Exception:
        return None


def _shallow_enabled_scan(text: str) -> dict | None:
    """No-PyYAML fallback: find plugins.enabled list items under 'enabled:'."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not line.startswith("plugins:") and stripped != "plugins:":
            continue
        # Look ahead for an enabled: key indented under plugins:
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if nxt.strip() == "":
                continue
            if nxt.startswith((" ", "\t")) and nxt.strip().startswith("enabled:"):
                items: list[str] = []
                for k in range(j + 1, len(lines)):
                    item = lines[k]
                    if item.startswith((" ", "\t")) and item.strip().startswith("- "):
                        items.append(item.strip()[2:].strip())
                    elif item.strip():
                        break
                return {"plugins": {"enabled": items}}
            if not nxt.startswith((" ", "\t")):
                break  # next top-level key, plugins has no enabled:
    return None


def _mnemosyne_in_enabled_config(home: Path) -> bool:
    data = _read_yaml_or_none(home / "config.yaml")
    if isinstance(data, dict):
        plugins = data.get("plugins")
        if isinstance(plugins, dict):
            enabled = plugins.get("enabled") or []
            if isinstance(enabled, (list, tuple)):
                return any(str(item).strip().lower() == "mnemosyne" for item in enabled)
    return False


# ── config mutation ──────────────────────────────────────────────────────


def desired_memlock_section(verdict: str, args: argparse.Namespace, harness_verdict: str = "hermes") -> dict:
    """The exact keys this tool owns. Nothing outside them is touched."""
    section: dict[str, Any] = {
        "reverse_audit": True,
        "preference_adapter": "" if verdict == "none" else verdict,
    }
    if verdict == "severian":
        dsn = os.environ.get("SEVERIAN_DSN", "").strip()
        if dsn:
            section["adapter_dsn"] = dsn
    elif verdict == "mnemosyne":
        db_path = _resolve_default_db_path()
        if db_path:
            section["adapter_db_path"] = db_path
    return section


def _resolve_default_db_path() -> str:
    for env_key in ("MNEMOSYNE_DB_PATH", "MNEMOSYNE_DATA_DIR"):
        val = os.environ.get(env_key, "").strip()
        if val:
            path = Path(val).expanduser()
            return str(path / "mnemosyne.db") if path.suffix != ".db" else str(path)
    return ""


def backup_config(home: Path) -> Path | None:
    src = home / "config.yaml"
    if not src.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.name}.bak-{stamp}")
    shutil.copy2(src, dst)
    return dst


def atomic_write(path: Path, data: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data)
    tmp.rename(path)


def merge_memlock_keys(home: Path, updates: dict[str, Any]) -> tuple[bool, str]:
    """Update ONLY keys inside the memlock: section of config.yaml.

    Returns (used_yaml_lib, message). With PyYAML the rest of the document is
    preserved object-for-object and rewritten; without it we refuse to touch
    the file and return manual instructions instead — an unparseable rewrite
    is worse than no write.
    """
    cfg_path = home / "config.yaml"
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return False, (
            "PyYAML not available — refusing to edit config.yaml blindly.\n"
            f"Manual step: add/update under '{cfg_path}':\n\nmemlock:\n"
            + "".join(
                f"  {key}: {_fmt_yaml_value(val)}\n"
                for key, val in updates.items()
            )
        )

    raw: dict = {}
    if cfg_path.exists():
        try:
            raw = yaml.safe_load(cfg_path.read_text()) or {}
        except Exception as exc:
            return False, (
                f"config.yaml exists but does not parse ({exc}); "
                "not touching it.\nManual step: add/update the memlock: "
                f"section with: {updates}"
            )
    if not isinstance(raw, dict):
        raw = {}
    memlock = raw.setdefault("memlock", {})
    if not isinstance(memlock, dict):
        memlock = {}
        raw["memlock"] = memlock

    changed = [k for k, v in updates.items() if memlock.get(k) != v]
    memlock.update(updates)
    atomic_write(cfg_path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    detail = (
        "updated keys: " + ", ".join(changed) if changed else "no changes needed"
    )
    return True, f"wrote {cfg_path} ({detail})"


def _fmt_yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value in ("", None):
        return '""'
    if isinstance(value, str):
        return value if re.fullmatch(r"[\w./:@-]+", value) else f'"{value}"'
    return str(value)


# ── pruning ──────────────────────────────────────────────────────────────


def prune_session_stores(home: Path, days: int, dry_run: bool) -> tuple[int, int]:
    """Delete memlock/<sid>.json files older than *days* by mtime.

    NEVER touches the persist/ subdirectory (durable global pins) or any
    non-.json file. Returns (count_deleted, bytes_freed).
    """
    memlock_dir = home / "memlock"
    if not memlock_dir.is_dir():
        return 0, 0
    cutoff = time.time() - days * 86400
    count = 0
    freed = 0
    for entry in sorted(memlock_dir.iterdir()):
        if not entry.is_file() or entry.name.endswith(".json.tmp"):
            continue
        # Session stores are exactly <name>.json directly under memlock/;
        # persist/ is a directory so it never matches is_file().
        if entry.suffix != ".json":
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            count += 1
            freed += stat.st_size
            if not dry_run:
                try:
                    entry.unlink()
                except OSError as exc:
                    print(f"warning: could not delete {entry}: {exc}", file=sys.stderr)
                    count -= 1
                    freed -= stat.st_size
    return count, freed


# ── main ─────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memlock_setup",
        description="Detect the memory provider and configure the memlock plugin.",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        default="auto",
        help="Override detection (default: auto)",
    )

    parser.add_argument(
        "--harness",
        choices=HARNESSES,
        default="auto",
        help="Select harness to configure (default: auto)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report and planned changes; write nothing",
    )
    parser.add_argument(
        "--prune-days",
        type=int,
        default=None,
        metavar="N",
        help="Delete session stores older than N days (min 7); "
             "never touches persist/",
    )
    args = parser.parse_args(argv)
    home = hermes_home()

    exit_code = 0

    # ── prune mode ───────────────────────────────────────────────────
    if args.prune_days is not None:
        if args.prune_days < 7:
            print(
                f"Error: --prune-days must be >= 7 (got {args.prune_days}). "
                "Recent session stores carry live audit state.",
                file=sys.stderr,
            )
            return 2
        count, freed = prune_session_stores(home, args.prune_days, args.dry_run)
        verb = "would delete" if args.dry_run else "deleted"
        print(
            f"Prune (>{args.prune_days} days old): {verb} {count} session "
            f"store(s), freeing {freed} bytes. persist/ untouched."
        )

    # ── detection ────────────────────────────────────────────────────
    detected, findings = detect_provider(home)
    verdict = detected if args.provider == "auto" else args.provider
    harness_verdict = args.harness if args.harness != "auto" else None
    if harness_verdict is None:
        # Auto-detect harness
        hermes_home_path = hermes_home()
        hermes_config = hermes_home_path / "config.yaml"
        claude_settings_cwd = Path(".claude/settings.json")
        claude_settings_home = Path.home() / ".claude/settings.json"
        
        if hermes_config.exists():
            harness_verdict = "hermes"
        elif claude_settings_cwd.exists() or claude_settings_home.exists():
            harness_verdict = "claude-code"
        else:
            harness_verdict = "hermes"
            print("  No Hermes or Claude Code detected; defaulting to Hermes config mode.")
            print("  Manual step: add/update the memlock: section in config.yaml")
    else:
        print(f"  Using --harness override: {harness_verdict}")


    print("MemLock setup")
    print(f"  HERMES_HOME: {home}")
    print()
    print("Detection findings:")
    for finding in findings:
        print(f"  - {finding}")
    print()
    if args.provider == "auto":
        print(f"Detected provider: {detected}")
    else:
        print(f"Detected provider: {detected} (overridden by --provider)")
    print(f"Using provider:   {verdict}")
    print(f"Using harness:    {harness_verdict}")
    if verdict == "none":
        print()
        print(
            "No memory provider detected. MemLock works fully without one:\n"
            "  - keyword/semantic anchor audits need nothing extra\n"
            "  - the reverse audit simply stays off (preference_adapter: \"\")\n"
            "Install Severian or Mnemosyne to enable preference-aware audits,\n"
            "or set --provider explicitly once a provider is in place."
        )

    # ── apply ────────────────────────────────────────────────────────
    if args.dry_run:
        planned = desired_memlock_section(verdict, args, harness_verdict)
        print()
        print("Dry run — would update the memlock: section of config.yaml with:")
        for key, val in planned.items():
            print(f"  {key}: {_fmt_yaml_value(val)}")
        if not (home / "config.yaml").exists():
            print(f"  (note: {home / 'config.yaml'} does not exist yet)")
        return exit_code

    # Always compute the updates first (may involve provider detection logic)
    updates = desired_memlock_section(verdict, args, harness_verdict)
    
    # Apply based on harness verdict
    if harness_verdict == "hermes":
        # Write to Hermes config.yaml (existing behavior)
        backup = backup_config(home)
        if backup:
            print(f"\nBacked up config to {backup.name}")
        used_yaml, message = merge_memlock_keys(home, updates)
        print(message)
    elif harness_verdict == "claude-code":
        # Apply Claude Code settings installer
        from shims.claude_code.settings_installer import apply_to_file
        import os
        claude_settings_path = home / ".claude" / "settings.json"
        claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
        final_settings = apply_to_file(str(claude_settings_path))
        print(f"Applied MemLock hooks to {claude_settings_path}")
        # Print what was installed (hooks section)
        hooks_section = final_settings.get("hooks", {})
        if hooks_section:
            print("Installed hooks:")
            for event, hook_list in hooks_section.items():
                print(f"  {event}: {len(hook_list)} hook(s)")
        else:
            print("No hooks installed")
    elif harness_verdict == "mcp":
        # Print MCP config snippet without writing config.yaml
        print()
        print("MCP configuration snippet:")
        print("Add this to your MCP client's configuration:")
        print()
        print('  "memlock": {')
        print('    "command": "python3",')
        print('    "args": ["-m", "mcp_server"],')
        print('    "transport": "stdio"')
        print("  }")
        print()
        print("Make sure the memlock MCP server is available in your PATH or")
        print("provide the full path to the mcp_server module.")
    else:
        # Fallback - write to config.yaml (should not happen with proper validation)
        backup = backup_config(home)
        if backup:
            print(f"\nBacked up config to {backup.name}")
        used_yaml, message = merge_memlock_keys(home, updates)
        print(message)

    if verdict == "none" and exit_code == 0 and detected == "none":
        exit_code = 1  # nothing detected: guidance printed, signal with 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
