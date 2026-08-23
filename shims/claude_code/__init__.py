"""MemLock shim for Claude Code — hook handlers + settings installer.

Claude Code runs shell commands at lifecycle events and passes a JSON payload
on stdin (``hook_event_name``, ``session_id``, ...). A handler may emit JSON
on stdout; ``hookSpecificOutput.additionalContext`` is injected into the
conversation, which is exactly the channel MemLock needs for reminder blocks.

Design constraints mirror memlock_core:
  - The handler functions here are PURE: parsed payload dict in → response
    dict out. Stdin/stdout/process wiring lives in ``main()`` so tests drive
    handlers directly with fabricated payloads.
  - Fail-open: malformed payloads, missing fields and service errors degrade
    to an empty response object (or exit-0 silence), never a crash — a broken
    protection layer must never break the host session.
  - Session identity is Claude Code's own ``session_id`` field.

Compaction boundary: Claude Code's continuation marker ("This session is
being continued from a previous conversation...") is passed to the core as
its ``summary_prefixes``, replacing the Hermes-shaped defaults.
"""
from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger(__name__)

try:  # package import (repo root on sys.path)
    from memlock_core import MemlockService
except ImportError:  # direct-script execution by a hook command
    from shims.memlock_core import MemlockService  # type: ignore[no-redef]

# Claude Code's compact-boundary marker: auto-compaction replaces the
# transcript with a summary message that starts with this literal.
CLAUDE_CODE_SUMMARY_PREFIX = (
    "This session is being continued from a previous conversation that ran "
    "out of context."
)

# Events this shim maps onto the core's lifecycle.
HANDLED_EVENTS = ("SessionStart", "PreCompact", "UserPromptSubmit")

# Shim version, tracked separately from the host plugin's version.
__version__ = "0.5.0"


def make_service(config: dict | None = None) -> "MemlockService":
    """Build a service wired with Claude Code's compaction marker."""
    return MemlockService(
        config,
        summary_prefixes=[CLAUDE_CODE_SUMMARY_PREFIX],
    )


class ClaudeCodeShim:
    """One service instance per harness process; pure handlers on top."""

    def __init__(self, config: dict | None = None) -> None:
        self.service = make_service(config)

    # ── pure hook handlers ───────────────────────────────────────────────

    def handle_session_start(self, payload: dict) -> dict:
        """Seed pins for session_id; return additionalContext when present."""
        session_id = str(payload.get("session_id", "") or "")
        if not session_id:
            return {}
        self.service.on_start(session_id=session_id)
        store = self.service.get_store(session_id)
        if store is None:
            return {}
        anchors = [
            a.get("reminder", a.get("text", ""))[:120]
            for a in store.sorted_anchors()
            if a.get("pinned")
        ]
        response: dict = {"suppressOutput": True}
        if anchors:
            lines = ["[memlock] pinned instructions active in this session:"]
            lines.extend(f"  - {a}" for a in anchors)
            response["hookSpecificOutput"] = {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(lines),
            }
        return response

    def handle_pre_compact(self, payload: dict) -> dict:
        """Snapshot state before compaction; no output contract to fulfil."""
        session_id = str(payload.get("session_id", "") or "")
        if not session_id:
            return {}
        store = self.service.get_store(session_id)
        if store is None:
            store = self.service.ensure_store(session_id)
        try:
            store.save()
            score = store.compute_integrity_score()
        except Exception as exc:
            logger.warning("memlock: pre-compact snapshot failed: %s", exc)
            return {}
        return {
            "suppressOutput": True,
            "systemMessage": (
                f"[memlock] state snapshotted before compaction "
                f"(session {session_id}, integrity "
                f"{'n/a' if score < 0 else f'{score}%'})."
            ),
        }

    def handle_user_prompt_submit(self, payload: dict) -> dict:
        """Audit post-compaction context; rehydrate casualties via additionalContext."""
        session_id = str(payload.get("session_id", "") or "")
        if not session_id:
            return {}
        prompt = str(payload.get("prompt", "") or "")
        transcript_path = str(payload.get("transcript_path", "") or "")
        history = _load_transcript_history(transcript_path)
        try:
            result = self.service.pre_llm_turn(
                session_id=session_id,
                user_message=prompt,
                conversation_history=history,
            )
        except Exception as exc:
            logger.warning("memlock: audit failed fail-open: %s", exc)
            result = None
        if not result or not isinstance(result, dict):
            return {"suppressOutput": True}
        block = result.get("context")
        if not block:
            return {"suppressOutput": True}
        return {
            "suppressOutput": True,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": block,
            },
        }

    # ── dispatch ─────────────────────────────────────────────────────────

    def handle_hook(self, event_name: str, payload: dict) -> dict:
        """Route one parsed hook payload to its handler. Unknown → {}."""
        if event_name == "SessionStart":
            return self.handle_session_start(payload)
        if event_name == "PreCompact":
            return self.handle_pre_compact(payload)
        if event_name == "UserPromptSubmit":
            return self.handle_user_prompt_submit(payload)
        logger.warning("memlock: ignoring unsupported hook event %r", event_name)
        return {}


def _load_transcript_history(transcript_path: str) -> list[dict]:
    """Best-effort read of a Claude Code transcript JSONL into history dicts.

    Each line is a JSON object; role/content-ish fields are projected onto the
    {role, content} shape the core understands. Any failure returns [] — the
    audit then sees only the current prompt (still correct, just narrower).
    """
    if not transcript_path:
        return []
    history: list[dict] = []
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                msg = entry.get("message") or entry
                role = str(msg.get("role") or entry.get("type") or "")
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    )
                if role:
                    history.append({"role": role, "content": content})
    except OSError as exc:
        logger.warning("memlock: transcript unreadable (%s); auditing prompt only", exc)
    return history


# ── process wiring (kept out of the pure paths) ─────────────────────────


def main(argv: list[str] | None = None) -> int:
    """Stdin → parse → handler → stdout. Always exits 0 (fail-open)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        logger.warning("memlock: unparseable hook payload (%s); no-op", exc)
        return 0
    if not isinstance(payload, dict):
        payload = {}
    event_name = str(payload.get("hook_event_name", "") or "")
    try:
        shim = ClaudeCodeShim()
        response = shim.handle_hook(event_name, payload)
    except Exception as exc:
        logger.warning("memlock: hook handling failed fail-open: %s", exc)
        return 0
    try:
        json.dump(response, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
    except Exception as exc:  # broken pipe etc. — never crash the host
        logger.warning("memlock: could not write hook response: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover - manual smoke only
    raise SystemExit(main())
