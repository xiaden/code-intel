from __future__ import annotations

import importlib.util
import json
import sys
import types
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest

HOOKS_DIR = Path(__file__).resolve().parents[1]
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

# ruff: noqa: E402
import shared_context.correlation as correlation_module
from shared_context.context_tools import context_add, context_read, context_shared
from shared_context.correlation import capture_pretooluse_spawn, correlate_subagent_start
from shared_context.normalizer import (
    JSONValue,
    extract_required,
    normalize_payload,
    normalize_required_pretooluse_fields,
    normalize_required_subagentstart_fields,
)
from shared_context.storage import SessionStorage, make_journal_record

_SESSION_COUNTER = count(1)


@pytest.fixture
def repo_root() -> Iterator[Path]:
    with TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


def _import_hook_module(name: str) -> types.ModuleType:
    """Import a hook script by filename (without .py extension)."""
    script_path = HOOKS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, str(script_path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _session_id(prefix: str) -> str:
    return f"{prefix}-{next(_SESSION_COUNTER):03d}"


def _storage(repo_root: Path, prefix: str) -> SessionStorage:
    return SessionStorage(session_id=_session_id(prefix), repo_root=repo_root)


def _journal_records(storage: SessionStorage, record_type: str) -> list[dict[str, object]]:
    return [record for record in storage.read_journal() if record.get("record_type") == record_type]


def _record_payload(record: dict[str, object]) -> dict[str, object]:
    payload = record.get("payload")
    assert isinstance(payload, dict)
    return payload


def _as_object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_string_list(value: object) -> list[str]:
    assert isinstance(value, list)
    assert all(isinstance(item, str) for item in value)
    return list(value)


def _json_dict(**values: JSONValue) -> dict[str, JSONValue]:
    return values


def _has_lint_invocation(records: list[dict[str, object]]) -> bool:
    lint_names = frozenset(
        {
            "lint_project_backend",
            "lint_project_frontend",
            "mcp_nomarr_dev_lint_project_backend",
            "mcp_nomarr_dev_lint_project_frontend",
        }
    )
    for record in records:
        if record.get("record_type") != "tool_invoked":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name.lower() in lint_names:
            return True
    return False


def _capture_spawn(
    storage: SessionStorage,
    *,
    tool_use_id: str,
    parent_agent_id: str = "parent-agent",
    parent_lineage: list[str] | None = None,
    tool_input_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    return capture_pretooluse_spawn(
        storage,
        session_id=storage.session_id,
        tool_use_id=tool_use_id,
        parent_agent_id=parent_agent_id,
        parent_lineage=parent_lineage or [],
        tool_input_summary=tool_input_summary or {"agentName": "Support-Researcher"},
        transcript_path="/tmp/transcript.jsonl",
        cwd="/tmp/workspace",
    )


# 1. Normalization tests


def test_normalization_snake_case_passthrough() -> None:
    payload: dict[str, object] = {
        "session_id": "session-001",
        "tool_use_id": "tool-001",
        "agent_id": "agent-001",
        "tool_name": "runSubagent",
        "tool_input": {"agent_name": "Support-Researcher"},
        "hook_event_name": "PreToolUse",
        "transcript_path": "/tmp/transcript.jsonl",
        "agent_type": "support",
    }

    normalized = normalize_payload(payload)

    assert normalized == payload


def test_normalization_camel_to_snake() -> None:
    payload: dict[str, object] = {
        "sessionId": "session-002",
        "toolUseId": "tool-002",
        "agentId": "agent-002",
        "toolName": "runSubagent",
        "toolInput": {"agentName": "Support-Researcher"},
        "hookEventName": "PreToolUse",
        "transcriptPath": "/tmp/transcript.jsonl",
        "agentType": "support",
    }

    normalized = normalize_payload(payload)

    assert normalized == {
        "session_id": "session-002",
        "tool_use_id": "tool-002",
        "agent_id": "agent-002",
        "tool_name": "runSubagent",
        "tool_input": {"agent_name": "Support-Researcher"},
        "hook_event_name": "PreToolUse",
        "transcript_path": "/tmp/transcript.jsonl",
        "agent_type": "support",
    }


def test_normalization_mixed_payload() -> None:
    payload: dict[str, object] = {
        "sessionId": "camel-session",
        "session_id": "snake-session",
        "toolUseId": "camel-tool",
        "tool_use_id": "snake-tool",
        "agentId": "camel-agent",
        "agent_id": "snake-agent",
        "toolName": "camel-name",
        "tool_name": "snake-name",
        "toolInput": {"agentName": "CamelAgent"},
        "tool_input": {"agent_name": "SnakeAgent"},
        "hookEventName": "CamelEvent",
        "hook_event_name": "SnakeEvent",
    }

    normalized = normalize_payload(payload)

    assert normalized == {
        "session_id": "snake-session",
        "tool_use_id": "snake-tool",
        "agent_id": "snake-agent",
        "tool_name": "snake-name",
        "tool_input": {"agent_name": "SnakeAgent"},
        "hook_event_name": "SnakeEvent",
    }


def test_normalization_tool_input_recursion() -> None:
    payload: dict[str, object] = {
        "toolInput": {
            "agentName": "Support-Researcher",
            "taskConfig": {
                "maxItems": 3,
                "childSteps": [{"stepName": "collectData"}],
            },
        }
    }

    normalized = normalize_payload(payload)

    assert normalized == {
        "tool_input": {
            "agent_name": "Support-Researcher",
            "task_config": {
                "max_items": 3,
                "child_steps": [{"step_name": "collectData"}],
            },
        }
    }


def test_extract_required_all_present() -> None:
    raw_payload: dict[str, object] = {
        "sessionId": "session-003",
        "toolUseId": "tool-003",
        "toolName": "runSubagent",
        "toolInput": {"agentName": "Support-Researcher"},
        "hookEventName": "PreToolUse",
    }
    payload = normalize_payload(raw_payload)

    extracted, missing = extract_required(
        payload,
        ["session_id", "tool_use_id", "tool_name", "tool_input", "hook_event_name"],
    )

    assert extracted == {
        "session_id": "session-003",
        "tool_use_id": "tool-003",
        "tool_name": "runSubagent",
        "tool_input": {"agent_name": "Support-Researcher"},
        "hook_event_name": "PreToolUse",
    }
    assert missing == []


def test_extract_required_partial_missing() -> None:
    raw_payload: dict[str, object] = {
        "sessionId": "session-004",
        "toolInput": {"agentName": "Support-Researcher"},
        "hookEventName": "PreToolUse",
    }
    payload = normalize_payload(raw_payload)

    extracted, missing = extract_required(
        payload,
        ["session_id", "tool_use_id", "tool_name", "tool_input", "hook_event_name"],
    )

    assert extracted == {
        "session_id": "session-004",
        "tool_input": {"agent_name": "Support-Researcher"},
        "hook_event_name": "PreToolUse",
    }
    assert missing == ["tool_use_id", "tool_name"]


# 2. Storage round-trip tests


def test_journal_append_and_read(repo_root: Path) -> None:
    storage = _storage(repo_root, "journal-roundtrip")
    record = make_journal_record(
        record_type="context_item_written",
        session_id=storage.session_id,
        journal_seq=1,
        agent_id="agent-a",
        correlation_id="corr-a",
        payload=_json_dict(item_id="ctx_001", delivery="sticky"),
    )

    storage.append_journal(cast("dict[str, object]", record))

    assert storage.read_journal() == [record]


def test_journal_lf_only(repo_root: Path) -> None:
    storage = _storage(repo_root, "journal-lf")
    record = make_journal_record(
        record_type="context_item_written",
        session_id=storage.session_id,
        journal_seq=1,
        agent_id="agent-a",
        correlation_id=None,
        payload=_json_dict(item_id="ctx_002", delivery="sticky"),
    )

    storage.append_journal(cast("dict[str, object]", record))
    raw_bytes = storage.journal_path.read_bytes()

    assert b"\r" not in raw_bytes
    assert raw_bytes.endswith(b"\n")


def test_journal_monotonic_seq(repo_root: Path) -> None:
    storage = _storage(repo_root, "journal-seq")
    written_seqs: list[int] = []
    for item_index in range(3):
        record = storage.append_journal_record(
            record_type="context_item_written",
            agent_id="agent-a",
            correlation_id=None,
            payload=_json_dict(item_id=f"ctx_seq_{item_index}", delivery="sticky"),
        )
        written_seqs.append(record["journal_seq"])

    assert written_seqs == [1, 2, 3]
    assert [record["journal_seq"] for record in storage.read_journal()] == [1, 2, 3]


def test_journal_malformed_line_skip(repo_root: Path) -> None:
    storage = _storage(repo_root, "journal-malformed")
    first_record = make_journal_record(
        record_type="context_item_written",
        session_id=storage.session_id,
        journal_seq=1,
        agent_id="agent-a",
        correlation_id=None,
        payload=_json_dict(item_id="ctx_first", delivery="sticky"),
    )
    second_record = make_journal_record(
        record_type="context_item_written",
        session_id=storage.session_id,
        journal_seq=2,
        agent_id="agent-b",
        correlation_id=None,
        payload=_json_dict(item_id="ctx_second", delivery="sticky"),
    )
    storage.append_journal(cast("dict[str, object]", first_record))

    with storage.journal_path.open("ab") as handle:
        handle.write(b"this is not json\n")
        handle.write(
            json.dumps(second_record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        )

    assert storage.read_journal() == [first_record, second_record]


def test_pending_envelope_write_read(repo_root: Path) -> None:
    storage = _storage(repo_root, "pending-roundtrip")
    envelope = {"correlation_id": "tool-001", "session_id": storage.session_id, "schema_version": 1}

    storage.write_pending_envelope("tool-001", envelope)

    assert storage.read_pending_envelope("tool-001") == envelope


def test_pending_envelope_write_once(repo_root: Path) -> None:
    storage = _storage(repo_root, "pending-once")
    envelope = {"correlation_id": "tool-002", "session_id": storage.session_id, "schema_version": 1}

    storage.write_pending_envelope("tool-002", envelope)

    with pytest.raises(FileExistsError):
        storage.write_pending_envelope("tool-002", envelope)


def test_active_envelope_write_read(repo_root: Path) -> None:
    storage = _storage(repo_root, "active-roundtrip")
    envelope = {"agent_id": "agent-child", "session_id": storage.session_id, "schema_version": 1}

    storage.write_active_envelope("agent-child", envelope)

    assert storage.read_active_envelope("agent-child") == envelope


def test_active_envelope_write_once(repo_root: Path) -> None:
    storage = _storage(repo_root, "active-once")
    envelope = {"agent_id": "agent-child", "session_id": storage.session_id, "schema_version": 1}

    storage.write_active_envelope("agent-child", envelope)

    with pytest.raises(FileExistsError):
        storage.write_active_envelope("agent-child", envelope)


def test_make_journal_record_schema(repo_root: Path) -> None:
    storage = _storage(repo_root, "journal-schema")
    record = make_journal_record(
        record_type="spawn_pending_written",
        session_id=storage.session_id,
        journal_seq=7,
        agent_id="agent-a",
        correlation_id="corr-007",
        payload=_json_dict(eligible_item_ids=cast("JSONValue", ["ctx_007"])),
    )

    assert set(record) == {
        "agent_id",
        "correlation_id",
        "journal_seq",
        "payload",
        "record_type",
        "session_id",
        "timestamp",
    }
    assert record["record_type"] == "spawn_pending_written"
    assert record["journal_seq"] == 7
    assert record["session_id"] == storage.session_id
    assert record["agent_id"] == "agent-a"
    assert record["correlation_id"] == "corr-007"
    assert record["payload"] == {"eligible_item_ids": ["ctx_007"]}
    assert isinstance(record["timestamp"], str)
    assert record["timestamp"].endswith("Z")


# 3. Context tool tests


def test_context_shared_writes_sticky_record(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-shared")

    context_shared(storage, "parent-a", [], "note", {"value": "sticky"})

    records = _journal_records(storage, "context_item_written")
    assert len(records) == 1
    assert _record_payload(records[0])["delivery"] == "sticky"


def test_context_add_writes_next_child_record(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-add-next-child")

    context_add(storage, "parent-a", [], "note", {"value": "one-shot"})

    records = _journal_records(storage, "context_item_written")
    assert len(records) == 1
    payload = _record_payload(records[0])
    assert payload["delivery"] == "next_child"
    assert payload["scope"] == "direct_child"


def test_context_add_explicit_sticky(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-add-sticky")

    context_add(storage, "parent-a", [], "note", {"value": "persist"}, delivery="sticky")

    records = _journal_records(storage, "context_item_written")
    assert len(records) == 1
    payload = _record_payload(records[0])
    assert payload["delivery"] == "sticky"
    assert payload["scope"] == "descendants"


def test_context_shared_replace_key_supersedes(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-supersede")
    first_item = context_shared(
        storage, "parent-a", [], "note", {"value": "first"}, replace_key="topic"
    )
    second_item = context_shared(
        storage, "parent-a", [], "note", {"value": "second"}, replace_key="topic"
    )

    superseded = _journal_records(storage, "context_item_superseded")

    assert len(superseded) == 1
    assert _record_payload(superseded[0]) == {
        "item_id": first_item["item_id"],
        "replace_key": "topic",
        "superseded_by": second_item["item_id"],
    }


def test_context_read_returns_sticky_items(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-sticky")
    item = context_shared(storage, "parent-a", [], "note", {"value": "hello"})

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert [entry["item_id"] for entry in result["items"]] == [item["item_id"]]
    assert result["items"][0]["payload"] == {"value": "hello"}


def test_context_read_excludes_superseded(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-superseded")
    first_item = context_shared(
        storage, "parent-a", [], "note", {"value": "first"}, replace_key="topic"
    )
    second_item = context_shared(
        storage, "parent-a", [], "note", {"value": "second"}, replace_key="topic"
    )

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert [entry["item_id"] for entry in result["items"]] == [second_item["item_id"]]
    assert all(entry["item_id"] != first_item["item_id"] for entry in result["items"])


def test_context_read_excludes_consumed_next_child(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-consumed")
    next_child_item = context_add(storage, "parent-a", [], "note", {"value": "single-use"})
    _capture_spawn(storage, tool_use_id="tool-100", parent_agent_id="parent-a")
    correlate_subagent_start(
        storage, subagent_session_id=storage.session_id, subagent_agent_id="tool-100"
    )

    parent_view = context_read(storage, current_agent_id="parent-a", current_lineage=[])

    assert next_child_item["item_id"] not in [entry["item_id"] for entry in parent_view["items"]]


def test_context_read_stable_ordering(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-order")
    first_item = context_shared(storage, "parent-a", [], "note", {"value": 1})
    second_item = context_shared(storage, "parent-a", [], "note", {"value": 2})
    third_item = context_shared(storage, "parent-a", [], "note", {"value": 3})

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert [entry["item_id"] for entry in result["items"]] == [
        first_item["item_id"],
        second_item["item_id"],
        third_item["item_id"],
    ]
    assert [entry["journal_seq"] for entry in result["items"]] == sorted(
        entry["journal_seq"] for entry in result["items"]
    )


def test_context_read_redacts_restricted(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-redacted")
    context_shared(storage, "parent-a", [], "note", {"token": "secret"}, sensitivity="restricted")

    journal_record = _journal_records(storage, "context_item_written")[0]
    journal_payload = _record_payload(journal_record)
    assert journal_payload["payload"] == {"redacted": True, "sensitivity": "restricted"}
    assert journal_payload["payload_redacted"] is True

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert result["items"][0]["payload"] == {"redacted": True, "sensitivity": "restricted"}


def test_context_read_max_items_truncation(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-truncate")
    first_item = context_shared(storage, "parent-a", [], "note", {"value": "one"})
    second_item = context_shared(storage, "parent-a", [], "note", {"value": "two"})
    context_shared(storage, "parent-a", [], "note", {"value": "three"})

    result = context_read(
        storage, current_agent_id="child-a", current_lineage=["parent-a"], max_items=2
    )

    assert [entry["item_id"] for entry in result["items"]] == [
        first_item["item_id"],
        second_item["item_id"],
    ]
    assert len(result["items"]) == 2


def test_context_read_tags_any_filter(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-tags")
    first_item = context_shared(storage, "parent-a", [], "note", {"value": "alpha"}, tags=["alpha"])
    context_shared(storage, "parent-a", [], "note", {"value": "beta"}, tags=["beta"])
    third_item = context_shared(
        storage, "parent-a", [], "note", {"value": "alpha-gamma"}, tags=["alpha", "gamma"]
    )

    result = context_read(
        storage, current_agent_id="child-a", current_lineage=["parent-a"], tags_any=["alpha"]
    )

    assert [entry["item_id"] for entry in result["items"]] == [
        first_item["item_id"],
        third_item["item_id"],
    ]


# 4. Correlation tests


def test_capture_pretooluse_spawn_creates_pending(repo_root: Path) -> None:
    storage = _storage(repo_root, "capture-pending")

    result = _capture_spawn(storage, tool_use_id="tool-201")

    assert result["status"] == "pending_created"
    pending = storage.read_pending_envelope("tool-201")
    assert pending is not None
    assert pending["correlation_id"] == "tool-201"
    assert pending["session_id"] == storage.session_id
    assert _journal_records(storage, "spawn_pending_written")


def test_capture_pretooluse_spawn_duplicate(repo_root: Path) -> None:
    storage = _storage(repo_root, "capture-duplicate")
    first = _capture_spawn(storage, tool_use_id="tool-202")
    second = _capture_spawn(storage, tool_use_id="tool-202")

    assert first["status"] == "pending_created"
    assert second["status"] == "duplicate"
    assert len(storage.list_pending_envelope_ids()) == 1
    duplicate_records = _journal_records(storage, "duplicate_event_ignored")
    assert len(duplicate_records) == 1
    assert _record_payload(duplicate_records[0])["reason"] == "pending_envelope_already_exists"


def test_correlate_subagent_start_activates(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-activate")
    sticky_item = context_shared(storage, "parent-a", [], "note", {"value": "carry"})
    _capture_spawn(storage, tool_use_id="tool-203", parent_agent_id="parent-a")

    result = correlate_subagent_start(
        storage,
        subagent_session_id=storage.session_id,
        subagent_agent_id="tool-203",
    )

    assert result["status"] == "activated"
    active = storage.read_active_envelope("tool-203")
    assert active is not None
    assert active["agent_id"] == "tool-203"
    assert active["correlation_id"] == "tool-203"
    effective_item_ids = _as_string_list(active["effective_item_ids"])
    assert sticky_item["item_id"] in effective_item_ids


def test_correlate_subagent_start_no_match(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-no-match")
    _capture_spawn(storage, tool_use_id="tool-204")

    result = correlate_subagent_start(
        storage,
        subagent_session_id="different-session",
        subagent_agent_id="tool-204",
    )

    assert result == {"status": "no_match"}
    assert storage.read_active_envelope("tool-204") is None


def test_correlate_subagent_start_wrong_agent_id(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-wrong-agent")
    _capture_spawn(storage, tool_use_id="tool-205")

    result = correlate_subagent_start(
        storage,
        subagent_session_id=storage.session_id,
        subagent_agent_id="tool-205-other",
    )

    assert result == {"status": "no_match"}
    assert storage.read_active_envelope("tool-205-other") is None


def test_correlate_subagent_start_duplicate_activation(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-duplicate")
    _capture_spawn(storage, tool_use_id="tool-206")
    first = correlate_subagent_start(
        storage, subagent_session_id=storage.session_id, subagent_agent_id="tool-206"
    )
    second = correlate_subagent_start(
        storage, subagent_session_id=storage.session_id, subagent_agent_id="tool-206"
    )

    assert first["status"] == "activated"
    assert second["status"] == "duplicate"
    assert storage.read_active_envelope("tool-206") is not None
    duplicate_records = _journal_records(storage, "duplicate_event_ignored")
    assert any(
        _record_payload(record).get("reason") == "active_envelope_already_exists"
        for record in duplicate_records
    )


def test_correlate_next_child_reservation_consumed(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-reserve-consume")
    next_child_item = context_add(storage, "parent-a", [], "note", {"value": "single-use"})

    _capture_spawn(storage, tool_use_id="tool-207", parent_agent_id="parent-a")
    reserved = _journal_records(storage, "next_child_reserved")
    assert len(reserved) == 1
    assert _record_payload(reserved[0])["item_id"] == next_child_item["item_id"]

    correlate_subagent_start(
        storage, subagent_session_id=storage.session_id, subagent_agent_id="tool-207"
    )
    consumed = _journal_records(storage, "context_item_consumed")

    assert len(consumed) == 1
    assert _record_payload(consumed[0]) == {
        "item_id": next_child_item["item_id"],
        "reserved_for": "tool-207",
    }


def test_correlate_next_child_not_in_later_spawn(repo_root: Path) -> None:
    storage = _storage(repo_root, "correlate-no-steal")
    next_child_item = context_add(storage, "parent-a", [], "note", {"value": "single-use"})

    first = _capture_spawn(storage, tool_use_id="tool-208-a", parent_agent_id="parent-a")
    second = _capture_spawn(storage, tool_use_id="tool-208-b", parent_agent_id="parent-a")

    first_pending = _as_object_dict(first["pending_envelope"])
    second_pending = _as_object_dict(second["pending_envelope"])
    first_eligible_item_ids = _as_string_list(first_pending["eligible_item_ids"])
    first_reserved_item_ids = _as_string_list(first_pending["reserved_next_child_item_ids"])
    second_eligible_item_ids = _as_string_list(second_pending["eligible_item_ids"])
    second_reserved_item_ids = _as_string_list(second_pending["reserved_next_child_item_ids"])

    assert next_child_item["item_id"] in first_eligible_item_ids
    assert next_child_item["item_id"] in first_reserved_item_ids
    assert next_child_item["item_id"] not in second_eligible_item_ids
    assert second_reserved_item_ids == []


# 5. Malformed/orphan tests


def test_malformed_event_missing_session_id(repo_root: Path) -> None:
    storage = _storage(repo_root, "anomaly-malformed")

    correlation_module._append_malformed_event(
        storage,
        agent_id="agent-a",
        correlation_id=None,
        reason="missing_session_id",
        details={"missing_fields": ["session_id"]},
    )

    malformed_records = _journal_records(storage, "malformed_event_ignored")
    assert len(malformed_records) == 1
    assert _record_payload(malformed_records[0]) == {
        "details": {"missing_fields": ["session_id"]},
        "reason": "missing_session_id",
    }


def test_orphaned_pending_no_child_start(repo_root: Path) -> None:
    storage = _storage(repo_root, "anomaly-orphaned")
    next_child_item = context_add(storage, "parent-a", [], "note", {"value": "single-use"})
    _capture_spawn(storage, tool_use_id="tool-209", parent_agent_id="parent-a")

    result = context_read(storage, current_agent_id="parent-a", current_lineage=[])

    assert next_child_item["item_id"] not in [entry["item_id"] for entry in result["items"]]


def test_context_read_redacts_ephemeral(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-ephemeral")
    context_shared(storage, "parent-a", [], "note", {"token": "temporary"}, sensitivity="ephemeral")

    journal_record = _journal_records(storage, "context_item_written")[0]
    journal_payload = _record_payload(journal_record)
    assert journal_payload["payload"] == {"redacted": True, "sensitivity": "ephemeral"}
    assert journal_payload["payload_redacted"] is True

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert result["items"][0]["payload"] == {"redacted": True, "sensitivity": "ephemeral"}


def test_context_read_excludes_expired_item(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-expired")
    context_shared(
        storage,
        "parent-a",
        [],
        "note",
        {"value": "stale"},
        expires_at="2000-01-01T00:00:00Z",
    )

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert result["items"] == []


def test_context_read_excludes_gated_item_without_gate_labels(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-gate-excluded")
    context_shared(storage, "parent-a", [], "note", {"value": "alpha"}, gate_label="alpha")

    result = context_read(storage, current_agent_id="child-a", current_lineage=["parent-a"])

    assert result["items"] == []


def test_context_read_includes_gated_item_with_matching_gate_label(repo_root: Path) -> None:
    storage = _storage(repo_root, "context-read-gate-included")
    item = context_shared(storage, "parent-a", [], "note", {"value": "alpha"}, gate_label="alpha")

    result = context_read(
        storage,
        current_agent_id="child-a",
        current_lineage=["parent-a"],
        gate_labels=["alpha"],
    )

    assert [entry["item_id"] for entry in result["items"]] == [item["item_id"]]


def test_normalize_required_pretooluse_fields_all_present() -> None:
    payload: dict[str, object] = {
        "sessionId": "session-010",
        "toolUseId": "tool-010",
        "toolName": "runSubagent",
        "toolInput": {"agentName": "Support-Researcher"},
        "hookEventName": "PreToolUse",
    }

    result = normalize_required_pretooluse_fields(payload)

    assert result["ok"] is True
    assert result["missing_fields"] == []
    assert result["values"] == {
        "session_id": "session-010",
        "tool_use_id": "tool-010",
        "tool_name": "runSubagent",
        "tool_input": {"agent_name": "Support-Researcher"},
        "hook_event_name": "PreToolUse",
    }


def test_normalize_required_pretooluse_fields_missing_required_values() -> None:
    payload: dict[str, object] = {
        "sessionId": "session-011",
        "toolInput": {"agentName": "Support-Researcher"},
        "hookEventName": "PreToolUse",
    }

    result = normalize_required_pretooluse_fields(payload)

    assert result["ok"] is False
    assert result["missing_fields"] == ["tool_use_id", "tool_name"]


def test_normalize_required_subagentstart_fields_all_present() -> None:
    payload: dict[str, object] = {
        "sessionId": "session-012",
        "agentId": "agent-012",
        "hookEventName": "SubagentStart",
    }

    result = normalize_required_subagentstart_fields(payload)

    assert result["ok"] is True
    assert result["missing_fields"] == []
    assert result["values"] == {
        "session_id": "session-012",
        "agent_id": "agent-012",
        "hook_event_name": "SubagentStart",
    }


class TestNewRecordTypes:
    """Tests for tool_invoked and tool_completed record types added in Phase 1."""

    def test_make_journal_record_accepts_tool_invoked(self) -> None:
        record = make_journal_record(
            record_type="tool_invoked",
            session_id="sess-001",
            journal_seq=1,
            agent_id="agent-001",
            correlation_id="corr-001",
            payload={
                "tool_name": "read_file",
                "tool_use_id": "tu-001",
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )
        assert record["record_type"] == "tool_invoked"

    def test_make_journal_record_accepts_tool_completed(self) -> None:
        record = make_journal_record(
            record_type="tool_completed",
            session_id="sess-001",
            journal_seq=2,
            agent_id="agent-001",
            correlation_id="corr-001",
            payload={"tool_use_id": "tu-001", "tool_name": "read_file", "status": "ok"},
        )
        assert record["record_type"] == "tool_completed"

    def test_make_journal_record_rejects_unknown_type(self) -> None:
        with pytest.raises(ValueError):
            make_journal_record(
                record_type="unknown_type",
                session_id="sess-001",
                journal_seq=3,
                agent_id="agent-001",
                correlation_id=None,
                payload={},
            )


class TestDetectErrorStatus:
    """Tests for _detect_error_status() from shared-context-posttooluse-all."""

    @pytest.fixture(scope="class")
    def posttooluse_all(self):  # type: ignore[no-untyped-def]
        return _import_hook_module("shared-context-posttooluse-all")

    def test_returns_error_for_error_colon(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("Error: something went wrong") == "error"

    def test_returns_error_case_insensitive(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("ERROR: caps") == "error"

    def test_returns_error_for_exception(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("Exception in thread main") == "error"

    def test_returns_error_for_traceback(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("Traceback (most recent call last):") == "error"

    def test_returns_error_for_failed(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("Failed to connect") == "error"

    def test_returns_ok_for_benign_output(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status("All tests passed\nDone.") == "ok"

    def test_returns_ok_for_none(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status(None) == "ok"

    def test_returns_ok_for_dict(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        assert posttooluse_all._detect_error_status({"key": "value"}) == "ok"

    def test_detects_error_in_multiline(self, posttooluse_all) -> None:  # type: ignore[no-untyped-def]
        response = "Starting up...\nError: connection refused\nDone"
        assert posttooluse_all._detect_error_status(response) == "error"


class TestExtractKeyInput:
    """Tests for _extract_key_input() from shared-context-pretooluse."""

    @pytest.fixture(scope="class")
    def pretooluse(self):  # type: ignore[no-untyped-def]
        return _import_hook_module("shared-context-pretooluse")

    def test_file_tool_returns_file_path(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("read_file", {"filePath": "/foo/bar.py"})
        assert result == "/foo/bar.py"

    def test_file_tool_snake_case_fallback(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("create_file", {"file_path": "/foo/baz.py"})
        assert result == "/foo/baz.py"

    def test_terminal_tool_returns_truncated_command(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("run_in_terminal", {"command": "echo hello world"})
        assert result == "echo hello world"

    def test_terminal_tool_truncates_at_80(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        long_cmd = "x" * 100
        result = pretooluse._extract_key_input("run_in_terminal", {"command": long_cmd})
        assert isinstance(result, str)
        assert len(result) <= 80

    def test_runsubagent_returns_dict(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input(
            "agent", {"agentName": "Exec-Executor", "description": "Do work"}
        )
        assert isinstance(result, dict)
        assert result.get("agentName") == "Exec-Executor"

    def test_lint_tool_returns_path(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("lint_project_backend", {"path": "nomarr/"})
        assert result == "nomarr/"

    def test_lint_tool_defaults_to_workspace(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("lint_project_backend", {})
        assert result == "workspace"

    def test_search_tool_returns_query(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("semantic_search", {"query": "find all hooks"})
        assert result == "find all hooks"

    def test_unknown_tool_returns_none(self, pretooluse) -> None:  # type: ignore[no-untyped-def]
        result = pretooluse._extract_key_input("some_other_tool", {"foo": "bar"})
        assert result is None


class TestSubagentStopLintGate:
    """Integration tests for SubagentStop lint gate logic using SessionStorage."""

    @pytest.fixture
    def repo_root_tmp(self) -> Iterator[Path]:
        with TemporaryDirectory() as temp_dir:
            yield Path(temp_dir)

    def _make_storage(self, repo_root: Path, session_id: str) -> SessionStorage:
        return SessionStorage(session_id=session_id, repo_root=repo_root)

    def test_blocks_when_no_lint_invocation(self, repo_root_tmp: Path) -> None:
        storage = self._make_storage(repo_root_tmp, "block-test-001")
        storage.append_journal_record(
            record_type="tool_invoked",
            agent_id="exec-executor",
            correlation_id="tu-001",
            payload={
                "tool_name": "read_file",
                "tool_use_id": "tu-001",
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )
        records = storage.read_journal()
        assert not _has_lint_invocation(records), "Should not have lint invocation"

    def test_allows_when_lint_invocation_present(self, repo_root_tmp: Path) -> None:
        storage = self._make_storage(repo_root_tmp, "allow-test-001")
        storage.append_journal_record(
            record_type="tool_invoked",
            agent_id="exec-executor",
            correlation_id="tu-002",
            payload={
                "tool_name": "lint_project_backend",
                "tool_use_id": "tu-002",
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )
        records = storage.read_journal()
        assert _has_lint_invocation(records), "Should have lint invocation → allow"

    def test_allows_mcp_variant_lint_tool(self, repo_root_tmp: Path) -> None:
        storage = self._make_storage(repo_root_tmp, "mcp-lint-test-001")
        storage.append_journal_record(
            record_type="tool_invoked",
            agent_id="qa-reviewer",
            correlation_id="tu-003",
            payload={
                "tool_name": "mcp_nomarr_dev_lint_project_backend",
                "tool_use_id": "tu-003",
                "timestamp": "2026-01-01T00:00:00Z",
            },
        )
        records = storage.read_journal()
        assert _has_lint_invocation(records)


# ---------------------------------------------------------------------------
# TestIsRunsubagentTool
# ---------------------------------------------------------------------------


class TestIsRunsubagentTool:
    """Tests for _is_runsubagent_tool() from shared-context-posttooluse-runsubagent."""

    @pytest.fixture(scope="class")
    def posttooluse_runsubagent(self) -> types.ModuleType:
        return _import_hook_module("shared-context-posttooluse-runsubagent")

    def test_agent_returns_true(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("agent") is True

    def test_camel_case_returns_true(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("runSubagent") is True

    def test_hyphenated_returns_true(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("run-subagent") is True

    def test_underscore_variant_returns_true(
        self, posttooluse_runsubagent: types.ModuleType
    ) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("run_subagent") is True

    def test_other_tool_returns_false(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("read_file") is False

    def test_none_returns_false(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool(None) is False

    def test_empty_string_returns_false(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("") is False

    def test_whitespace_only_returns_false(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool("   ") is False

    def test_non_string_returns_false(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._is_runsubagent_tool(123) is False


# ---------------------------------------------------------------------------
# TestFormatKeyInput
# ---------------------------------------------------------------------------


class TestFormatKeyInput:
    """Tests for _format_key_input() from shared-context-posttooluse-runsubagent."""

    @pytest.fixture(scope="class")
    def posttooluse_runsubagent(self) -> types.ModuleType:
        return _import_hook_module("shared-context-posttooluse-runsubagent")

    def test_none_returns_empty(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._format_key_input(None) == ""

    def test_dict_agent_name(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"agentName": "Exec-Executor"})
        assert result == "agentName=Exec-Executor"

    def test_dict_command(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"command": "echo hello"})
        assert result == "command=echo hello"

    def test_dict_query(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"query": "find hooks"})
        assert result == "query=find hooks"

    def test_dict_path(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"path": "/some/path"})
        assert result == "path=/some/path"

    def test_dict_file_path_camel(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"filePath": "/file.py"})
        assert result == "filePath=/file.py"

    def test_dict_file_path_snake(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"file_path": "/file.py"})
        assert result == "file_path=/file.py"

    def test_dict_unknown_key_fallback(self, posttooluse_runsubagent: types.ModuleType) -> None:
        result = posttooluse_runsubagent._format_key_input({"custom_key": "value"})
        assert result == "custom_key=value"

    def test_empty_dict_returns_empty(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._format_key_input({}) == ""

    def test_scalar_string(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._format_key_input("hello") == "hello"

    def test_scalar_int(self, posttooluse_runsubagent: types.ModuleType) -> None:
        assert posttooluse_runsubagent._format_key_input(42) == "42"

    def test_dict_value_truncated_at_40(self, posttooluse_runsubagent: types.ModuleType) -> None:
        long_val = "x" * 60
        result = posttooluse_runsubagent._format_key_input({"path": long_val})
        assert result == "path=" + "x" * 40

    def test_scalar_truncated_at_60(self, posttooluse_runsubagent: types.ModuleType) -> None:
        long_val = "y" * 80
        result = posttooluse_runsubagent._format_key_input(long_val)
        assert result == "y" * 60


# ---------------------------------------------------------------------------
# TestBuildSummary
# ---------------------------------------------------------------------------


class TestBuildSummary:
    """Tests for _build_summary() from shared-context-posttooluse-runsubagent."""

    @pytest.fixture(scope="class")
    def posttooluse_runsubagent(self) -> types.ModuleType:
        return _import_hook_module("shared-context-posttooluse-runsubagent")

    @staticmethod
    def _invoked(tool_name: str, tool_use_id: str, key_input: object = None) -> dict[str, object]:
        payload: dict[str, object] = {"tool_name": tool_name, "tool_use_id": tool_use_id}
        if key_input is not None:
            payload["key_input"] = key_input
        return {"record_type": "tool_invoked", "payload": payload}

    @staticmethod
    def _completed(tool_use_id: str, status: str) -> dict[str, object]:
        return {
            "record_type": "tool_completed",
            "payload": {"tool_use_id": tool_use_id, "status": status},
        }

    def test_empty_records_returns_header_only(
        self, posttooluse_runsubagent: types.ModuleType
    ) -> None:
        result = posttooluse_runsubagent._build_summary("MyAgent", [])
        assert result == "[MyAgent tool audit]"

    def test_ok_completion_shows_checkmark(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [
            self._invoked("read_file", "tu-1"),
            self._completed("tu-1", "ok"),
        ]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        lines = result.splitlines()
        assert lines[1] == "✓ read_file"

    def test_error_completion_shows_cross(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [
            self._invoked("read_file", "tu-2"),
            self._completed("tu-2", "error"),
        ]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        lines = result.splitlines()
        assert lines[1] == "✗ read_file"

    def test_missing_completion_shows_cross(
        self, posttooluse_runsubagent: types.ModuleType
    ) -> None:
        records: list[dict[str, object]] = [self._invoked("run_in_terminal", "tu-3")]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        lines = result.splitlines()
        assert lines[1] == "✗ run_in_terminal"

    def test_key_input_appears_in_parens(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [
            self._invoked("read_file", "tu-4", "/foo/bar.py"),
            self._completed("tu-4", "ok"),
        ]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        assert "(/foo/bar.py)" in result

    def test_no_key_input_no_parens(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [
            self._invoked("read_file", "tu-5"),
            self._completed("tu-5", "ok"),
        ]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        assert "(" not in result.splitlines()[1]

    def test_non_dict_payload_skipped(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [{"record_type": "tool_invoked", "payload": "bad"}]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        assert result == "[MyAgent tool audit]"

    def test_multiple_records_ordered(self, posttooluse_runsubagent: types.ModuleType) -> None:
        records: list[dict[str, object]] = [
            self._invoked("read_file", "tu-6"),
            self._invoked("run_in_terminal", "tu-7"),
            self._completed("tu-6", "ok"),
            self._completed("tu-7", "error"),
        ]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        lines = result.splitlines()
        assert lines[0] == "[MyAgent tool audit]"
        assert "read_file" in lines[1]
        assert "run_in_terminal" in lines[2]

    def test_orphan_completed_not_in_output(
        self, posttooluse_runsubagent: types.ModuleType
    ) -> None:
        records: list[dict[str, object]] = [self._completed("tu-orphan", "ok")]
        result = posttooluse_runsubagent._build_summary("MyAgent", records)
        assert result == "[MyAgent tool audit]"


# ---------------------------------------------------------------------------
# TestNormalizeAgentType
# ---------------------------------------------------------------------------


class TestNormalizeAgentType:
    """Tests for _normalize_agent_type() from shared-context-subagentstop."""

    @pytest.fixture(scope="class")
    def subagentstop(self) -> types.ModuleType:
        return _import_hook_module("shared-context-subagentstop")

    def test_underscore_becomes_hyphen(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("exec_executor") == "exec-executor"

    def test_hyphen_unchanged(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("exec-executor") == "exec-executor"

    def test_uppercase_lowercased(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("EXEC-EXECUTOR") == "exec-executor"

    def test_mixed_case_and_underscore(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("Qa_TestGenerator") == "qa-testgenerator"

    def test_none_returns_empty(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type(None) == ""

    def test_empty_string_returns_empty(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("") == ""

    def test_whitespace_only_returns_empty(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type("   ") == ""

    def test_non_string_returns_empty(self, subagentstop: types.ModuleType) -> None:
        assert subagentstop._normalize_agent_type(42) == ""
