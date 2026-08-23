"""Settings registration for the Claude Code shim.

Produces the ``hooks`` JSON snippet for a project's ``.claude/settings.json``
and merges it into an existing file WITHOUT clobbering unrelated keys
(permissions, env, other hooks...). Writes are atomic: tmp file + rename.

The hook commands run this package's ``main()`` entrypoint with the repo root
on ``PYTHONPATH``; ``$CLAUDE_PROJECT_DIR`` (exported by Claude Code during
hook execution) keeps the command working from any subdirectory.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # package import (repo root on sys.path)
    from shims.claude_code import HANDLED_EVENTS, __version__ as _shim_version
except ImportError:  # direct-script execution by a hook command
    from claude_code import HANDLED_EVENTS  # type: ignore[no-redef]
    __version__ = "0.5.0"  # type: ignore[no-redef]

HOOK_COMMAND = (
    "PYTHONPATH=\"$CLAUDE_PROJECT_DIR/memlock\" "
    "python3 -m shims.claude_code"
)


def hooks_snippet() -> dict:
    """The hooks registration object to merge under settings['hooks'].

    Shape follows Claude Code's documented settings contract:
    ``hooks.<Event> = [{"matcher": ..., "hooks": [{"type": "command",
    "command": ...}]}]`` — one matcher-entry LIST per event MemLock maps;
    PreCompact and UserPromptSubmit match all triggers.
    """
    matcher_entry: dict[str, Any] = {
        "matcher": "*",
        "hooks": [{
            "type": "command",
            "command": HOOK_COMMAND,
        }],
    }
    return {
        "hooks": {event: [dict(matcher_entry)] for event in HANDLED_EVENTS},
    }


def merge_settings(existing: dict | None) -> dict:
    """Return existing settings overlaid with the MemLock hook registration.

    Only ``settings["hooks"]`` is written. Existing top-level keys survive;
    an already-registered identical MemLock command is not duplicated, while
    any OTHER hooks (user's own or older wording) are preserved untouched —
    never clobber, never remove what we did not write.
    """
    merged = dict(existing or {})
    hooks = dict(merged.get("hooks") or {})
    ours = hooks_snippet()["hooks"]
    for event, entry in ours.items():
        current = [e for e in (hooks.get(event) or []) if isinstance(e, dict)]
        already = any(
            any(
                isinstance(h, dict) and h.get("command") == HOOK_COMMAND
                for h in (e.get("hooks") or [])
                if isinstance(e.get("hooks"), list)
            )
            for e in current
        )
        if not already:
            current.append(entry[0])
        hooks[event] = current
    merged["hooks"] = hooks
    return merged


def apply_to_file(settings_path: str | os.PathLike) -> dict:
    """Merge the hook registration into a settings.json file, atomically.

    Missing parent dirs are created; unparseable JSON fails open with a
    warning and starts a fresh settings object rather than crashing the
    installer; a .bak copy of the previous content is kept alongside for
    recovery. Returns the final merged settings dict actually on disk.
    """
    path = Path(settings_path)
    existing: dict = {}
    raw = None
    try:
        raw = path.read_text()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("memlock: settings unreadable (%s); writing fresh", exc)

    if raw is not None:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                existing = decoded
            else:
                logger.warning(
                    "memlock: settings root is %s, expected object; "
                    "starting fresh (backup kept)",
                    type(decoded).__name__,
                )
        except ValueError as exc:
            logger.warning(
                "memlock: corrupt settings JSON (%s); starting fresh "
                "(backup kept)", exc,
            )

    final = merge_settings(existing)
    payload = json.dumps(final, indent=2, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            try:
                (path.with_suffix(path.suffix + ".bak")).write_text(raw)
            except OSError as exc:
                logger.warning("memlock: could not keep settings backup: %s", exc)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, str(path))
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        # Fail-open: report instead of crashing the setup wizard.
        logger.warning("memlock: settings write failed: %s", exc)
        return final
    return final


def read_settings(settings_path: str | os.PathLike) -> dict:
    """Best-effort read of a settings.json (missing/corrupt → {})."""
    try:
        decoded = json.loads(Path(settings_path).read_text())
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}
