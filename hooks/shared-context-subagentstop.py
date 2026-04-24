#!/usr/bin/env python3
"""Shared-context SubagentStop hook — lint gate for executor-class agents.

Reads hook JSON from stdin, writes hook response JSON to stdout.
Blocks executor agents from stopping if they haven't invoked a lint tool.
Always fails open (allow) when session state is unknown or unreadable.
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

_HOOK_EVENT_NAME: Final[str] = "SubagentStop"

_EXECUTOR_AGENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "exec-executor",
        "exec-manager",
        "exec-fixer",
        "qa-reviewer",
        "qa-testgenerator",
        "qa-docsgenerator",
    }
)

_LINT_TOOL_NAMES_LOWER: Final[frozenset[str]] = frozenset(
    {
        "lint_project_backend",
        "lint_project_frontend",
        "mcp_nomarr_dev_lint_project_backend",
        "mcp_nomarr_dev_lint_project_frontend",
    }
)


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


def _normalize_agent_type(value: object) -> str:
    """Normalize agent type to lowercase with hyphens for lookup."""

    normalized = _normalize_non_empty_string(value)
    if normalized is None:
        return ""
    return normalized.lower().replace("_", "-")


def _allow() -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT_NAME,
            "permissionDecision": "allow",
        }
    }


def _block(reason: str) -> dict[str, object]:
    return {
        "hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT_NAME,
            "permissionDecision": "block",
            "reason": reason,
        }
    }


def _emit_allow() -> int:
    print(json.dumps(_allow(), ensure_ascii=False))
    return 0


def _emit_block(reason: str) -> int:
    print(json.dumps(_block(reason), ensure_ascii=False))
    return 0


def main() -> int:
    """Enforce the lint gate for executor-class agents on SubagentStop."""

    raw_payload = _read_stdin_json()
    if not raw_payload:
        return _emit_allow()

    try:
        payload = normalize_payload(raw_payload)
    except Exception:
        return _emit_allow()

    if not _event_matches(payload, _HOOK_EVENT_NAME):
        return _emit_allow()

    stop_hook_active = payload.get("stop_hook_active")
    if stop_hook_active:
        return _emit_allow()

    agent_type = _normalize_agent_type(payload.get("agent_type"))
    if agent_type not in _EXECUTOR_AGENT_TYPES:
        return _emit_allow()

    session_id = _normalize_non_empty_string(payload.get("session_id"))
    if session_id is None:
        return _emit_allow()

    try:
        storage = SessionStorage(session_id=session_id, repo_root=_repo_root())
        records = storage.read_journal()
    except Exception:
        return _emit_allow()

    for record in records:
        if record.get("record_type") != "tool_invoked":
            continue
        record_payload = record.get("payload")
        if not isinstance(record_payload, dict):
            continue
        tool_name = record_payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name.lower() in _LINT_TOOL_NAMES_LOWER:
            return _emit_allow()

    return _emit_block(
        "Executor agent must run lint_project_backend before exiting. Run it now and then stop."
    )


if __name__ == "__main__":
    raise SystemExit(main())
