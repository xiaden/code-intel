#!/usr/bin/env python3
"""Shared-context PostToolUse hook for all tools — appends tool_completed records.

Reads hook JSON from stdin, writes hook response JSON to stdout.
Records a tool_completed journal entry for every tool call that completes.
Always emits allow — journal failures never block tool execution.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Final

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

# ruff: noqa: E402
from shared_context import SessionStorage, normalize_payload
from shared_context.storage import JSONValue

_HOOK_EVENT_NAME: Final[str] = "PostToolUse"


def _read_stdin_json() -> dict[str, object]:
    """Read a hook payload from stdin, returning an empty dict on any issue."""
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _repo_root() -> Path:
    """Resolve repository root from this script location."""
    return Path(__file__).resolve().parents[2]


def _normalize_non_empty_string(value: object) -> str | None:
    """Return a trimmed non-empty string, or None when unusable."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _event_matches(payload: dict[str, object], expected_name: str) -> bool:
    """Return whether the normalized hook event name matches case-insensitively."""
    hook_event_name = _normalize_non_empty_string(payload.get("hook_event_name"))
    if hook_event_name is None:
        return False
    return hook_event_name.casefold() == expected_name.casefold()


def _allow() -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT_NAME,
            "permissionDecision": "allow",
        }
    }


def _emit_allow() -> int:
    print(json.dumps(_allow(), ensure_ascii=False))
    return 0


def _detect_error_status(tool_response: object) -> str:
    """Return 'error' if tool_response contains error indicators, 'ok' otherwise.

    Checks each line case-insensitively for lines starting with:
    'error:', 'exception', 'traceback', or 'failed'.
    """
    response_str = str(tool_response)
    for line in response_str.split("\n"):
        line_stripped = line.strip().lower()
        if line_stripped.startswith(("error:", "exception", "traceback", "failed")):
            return "error"
    return "ok"


def main() -> int:
    """Append tool_completed journal record for every completed tool call."""
    raw_payload = _read_stdin_json()
    if not raw_payload:
        return _emit_allow()

    try:
        payload = normalize_payload(raw_payload)
    except Exception:
        return _emit_allow()

    if not _event_matches(payload, _HOOK_EVENT_NAME):
        return _emit_allow()

    session_id = _normalize_non_empty_string(payload.get("session_id"))
    tool_use_id = _normalize_non_empty_string(payload.get("tool_use_id"))
    tool_name = _normalize_non_empty_string(payload.get("tool_name")) or ""
    agent_id = _normalize_non_empty_string(payload.get("agent_id")) or (
        f"root:{session_id}" if session_id else "root:unknown"
    )
    tool_response = payload.get("tool_response")
    status = _detect_error_status(tool_response)

    if session_id and tool_use_id:
        try:
            completed_payload: dict[str, JSONValue] = {
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "status": status,
            }
            SessionStorage(session_id=session_id, repo_root=_repo_root()).append_journal_record(
                record_type="tool_completed",
                agent_id=agent_id,
                correlation_id=tool_use_id,
                payload=completed_payload,
            )
        except Exception:
            pass

    return _emit_allow()


if __name__ == "__main__":
    raise SystemExit(main())
