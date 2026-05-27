#!/usr/bin/env python3
"""Shared-context PostToolUse hook for runSubagent — injects child tool-audit summary.

Reads hook JSON from stdin, writes hook response JSON to stdout.
After a runSubagent call completes, reads the parent session journal to find
tool_invoked/tool_completed records for the child agent and returns a compact
summary via additionalContext. Always emits allow — failures never block continuation.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Final

HOOKS_DIR = Path(__file__).resolve().parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

# ruff: noqa: E402
from shared_context import SessionStorage, cleanup_stale_sessions, normalize_payload

_HOOK_EVENT_NAME: Final[str] = "PostToolUse"
_RUNSUBAGENT_TOOL_NAMES: Final[frozenset[str]] = frozenset({"agent", "run_subagent", "runsubagent"})


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


def _is_runsubagent_tool(tool_name: object) -> bool:
    """Return whether the tool name maps to a runSubagent call."""
    normalized = _normalize_non_empty_string(tool_name)
    if normalized is None:
        return False
    return normalized.lower().replace("-", "_") in _RUNSUBAGENT_TOOL_NAMES


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


def _format_key_input(key_input: object) -> str:
    """Return a display string for key_input in summary lines."""
    if key_input is None:
        return ""
    if isinstance(key_input, dict):
        # Pick the most descriptive value from the dict
        for k in ("agentName", "agent_name", "command", "query", "path", "filePath", "file_path"):
            v = key_input.get(k)
            if v:
                return f"{k}={str(v)[:40]}"
        # Fall back to first non-empty value
        for k, v in key_input.items():
            if v:
                return f"{k}={str(v)[:40]}"
        return ""
    # Scalar (str, int, etc.)
    return str(key_input)[:60]


def _build_summary(
    agent_name: str,
    child_records: list[dict[str, object]],
) -> str:
    """Build a compact tool-audit summary string for the child agent."""
    # Separate invoked and completed records
    invoked: list[dict[str, object]] = []
    completed_map: dict[str, dict[str, object]] = {}  # keyed by tool_use_id

    for record in child_records:
        record_type = record.get("record_type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record_type == "tool_invoked":
            invoked.append(record)
        elif record_type == "tool_completed":
            tuid = _normalize_non_empty_string(payload.get("tool_use_id"))
            if tuid:
                completed_map[tuid] = record

    lines: list[str] = [f"[{agent_name} tool audit]"]

    for inv_record in invoked:
        inv_payload = inv_record.get("payload")
        if not isinstance(inv_payload, dict):
            continue
        inv_tool_name = str(inv_payload.get("tool_name") or "")
        inv_tool_use_id = _normalize_non_empty_string(inv_payload.get("tool_use_id"))
        key_input = inv_payload.get("key_input")
        key_str = _format_key_input(key_input)

        # Find the matching completed record
        completed = completed_map.get(inv_tool_use_id) if inv_tool_use_id else None
        if completed is not None:
            comp_payload = completed.get("payload")
            status = str(comp_payload.get("status") or "") if isinstance(comp_payload, dict) else ""
        else:
            status = "error"  # no completion record = likely failed

        check = "✓" if status == "ok" else "✗"
        if key_str:
            lines.append(f"{check} {inv_tool_name} ({key_str})")
        else:
            lines.append(f"{check} {inv_tool_name}")

    return "\n".join(lines)


def _maybe_cleanup_stale_sessions(repo_root: Path) -> None:
    """Delete stale session journals at most once per hour to prevent bloat.

    Uses a marker file mtime as a lightweight throttle so cleanup runs at most
    once per hour regardless of how many subagents complete in a session.
    """
    marker = repo_root / "artifacts" / "scratch" / "shared-context" / "v1" / ".last_cleanup"
    try:
        if marker.exists() and (time.time() - marker.stat().st_mtime) < 3600:
            return
        cleanup_stale_sessions(repo_root, max_age_days=1.0)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:  # noqa: BLE001
        pass  # cleanup is best-effort; never block the hook


def main() -> int:
    """Build and inject a child tool-audit summary after runSubagent completes."""
    raw_payload = _read_stdin_json()
    if not raw_payload:
        return _emit_allow()

    try:
        payload = normalize_payload(raw_payload)
    except Exception:
        return _emit_allow()

    if not _event_matches(payload, _HOOK_EVENT_NAME):
        return _emit_allow()

    tool_name = _normalize_non_empty_string(payload.get("tool_name"))
    if not _is_runsubagent_tool(tool_name):
        return _emit_allow()

    session_id = _normalize_non_empty_string(payload.get("session_id"))
    tool_use_id = _normalize_non_empty_string(payload.get("tool_use_id"))

    if not session_id or not tool_use_id:
        return _emit_allow()

    # child_agent_id == tool_use_id: the child's journal records carry the
    # tool_use_id as their agent_id (correlation key set by PreToolUse hook).
    child_agent_id = tool_use_id

    try:
        storage = SessionStorage(session_id=session_id, repo_root=_repo_root())
        all_records = storage.read_journal_tail(1000)
    except Exception:
        return _emit_allow()

    child_records = [
        record
        for record in all_records
        if (
            record.get("agent_id") == child_agent_id
            and record.get("record_type") in {"tool_invoked", "tool_completed"}
        )
    ]

    if not child_records:
        return _emit_allow()

    # Determine agent_name from tool_input
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        agent_name = (
            _normalize_non_empty_string(tool_input.get("agent_name"))
            or _normalize_non_empty_string(tool_input.get("agentName"))
            or "subagent"
        )
    else:
        agent_name = "subagent"

    try:
        summary = _build_summary(agent_name, child_records)
    except Exception:
        return _emit_allow()

    response: dict[str, object] = {
        "hookSpecificOutput": {
            "hookEventName": _HOOK_EVENT_NAME,
            "permissionDecision": "allow",
            "additionalContext": summary,
        }
    }
    print(json.dumps(response, ensure_ascii=False))
    _maybe_cleanup_stale_sessions(_repo_root())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
