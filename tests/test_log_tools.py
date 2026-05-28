"""Tests for log tools (log_write + log_read) using JSONL storage.

Covers:
- log_write: first write creates file, subsequent appends, invalid
    agent/category/title, tags, body, round-trip
- log_read: newest-first, filters by category/tag/title_query/since/until,
    combined filters, limit, not found, wildcard ("*")
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp_code_intel.helpers.log_jsonl import LOGS_DIR, LogEntry, append_entry
from mcp_code_intel.tools.log_read import log_read
from mcp_code_intel.tools.log_write import log_write

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_entry(
    tmp_path: Path,
    agent: str = "test-agent",
    title: str = "Test entry",
    category: str = "research",
    body: str = "",
    tags: list[str] | None = None,
) -> dict:
    return log_write(
        agent=agent,
        title=title,
        category=category,
        body=body,
        tags=tags,
        workspace_root=tmp_path,
    )


def _inject_entry(
    tmp_path: Path,
    agent: str,
    entry_id: str,
    ts: str,
    category: str = "research",
    title: str = "Injected",
    tags: list[str] | None = None,
    body: str = "",
) -> None:
    """Write a JSONL entry with a controlled timestamp, bypassing log_write."""
    log_file = tmp_path / LOGS_DIR / f"{agent}.log.jsonl"
    entry = LogEntry(
        id=entry_id,
        ts=ts,
        category=category,
        title=title,
        tags=tags or [],
        body=body,
    )
    append_entry(log_file, entry)


# ---------------------------------------------------------------------------
# log_write
# ---------------------------------------------------------------------------


def test_log_write_creates_file(tmp_path: Path) -> None:
    result = _write_entry(tmp_path)
    assert "path" in result
    assert result["entry_id"] == "L1"
    log_file = tmp_path / LOGS_DIR / "test-agent.log.jsonl"
    assert log_file.exists()


def test_log_write_file_contains_valid_jsonl(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="Hello")
    log_file = tmp_path / LOGS_DIR / "test-agent.log.jsonl"
    lines = [ln for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["title"] == "Hello"
    assert obj["ts"].endswith("Z")


def test_log_write_subsequent_appends(tmp_path: Path) -> None:
    r1 = _write_entry(tmp_path, title="First")
    r2 = _write_entry(tmp_path, title="Second")
    assert r1["entry_id"] == "L1"
    assert r2["entry_id"] == "L2"


def test_log_write_invalid_agent(tmp_path: Path) -> None:
    result = _write_entry(tmp_path, agent="Bad Agent!")
    assert result["error"] == "invalid_agent"


def test_log_write_invalid_category(tmp_path: Path) -> None:
    result = _write_entry(tmp_path, category="invalid-cat")
    assert result["error"] == "invalid_category"


def test_log_write_empty_title(tmp_path: Path) -> None:
    result = _write_entry(tmp_path, title="")
    assert result["error"] == "invalid_title"


def test_log_write_whitespace_title(tmp_path: Path) -> None:
    result = _write_entry(tmp_path, title="   ")
    assert result["error"] == "invalid_title"


def test_log_write_with_tags(tmp_path: Path) -> None:
    _write_entry(tmp_path, tags=["tag1", "tag2"])
    log_file = tmp_path / LOGS_DIR / "test-agent.log.jsonl"
    obj = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert obj["tags"] == ["tag1", "tag2"]


def test_log_write_with_body(tmp_path: Path) -> None:
    _write_entry(tmp_path, body="Detailed body text.")
    log_file = tmp_path / LOGS_DIR / "test-agent.log.jsonl"
    obj = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert obj["body"] == "Detailed body text."


def test_log_write_round_trip(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="Research entry", category="research", tags=["db"], body="Found.")
    _write_entry(tmp_path, title="Decision entry", category="decision", body="Decided.")
    result = log_read(agent="test-agent", workspace_root=tmp_path)
    assert result["entries"][0]["title"] == "Decision entry"
    assert result["entries"][1]["title"] == "Research entry"


# ---------------------------------------------------------------------------
# log_read — basic filters
# ---------------------------------------------------------------------------


def test_log_read_newest_first(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="First")
    _write_entry(tmp_path, title="Second")
    result = log_read(agent="test-agent", workspace_root=tmp_path)
    assert result["entries"][0]["title"] == "Second"
    assert result["entries"][1]["title"] == "First"


def test_log_read_filter_by_category(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="Research", category="research")
    _write_entry(tmp_path, title="Decision", category="decision")
    result = log_read(agent="test-agent", category="decision", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "Decision"


def test_log_read_filter_by_tag(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="Tagged", tags=["db", "ml"])
    _write_entry(tmp_path, title="Untagged")
    result = log_read(agent="test-agent", tag="db", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "Tagged"


def test_log_read_filter_by_title_query(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="Important discovery")
    _write_entry(tmp_path, title="Routine check")
    result = log_read(agent="test-agent", title_query="discovery", workspace_root=tmp_path)
    assert result["total"] == 1


def test_log_read_combined_filters(tmp_path: Path) -> None:
    _write_entry(tmp_path, title="DB Research", category="research", tags=["db"])
    _write_entry(tmp_path, title="ML Research", category="research", tags=["ml"])
    _write_entry(tmp_path, title="DB Decision", category="decision", tags=["db"])
    result = log_read(agent="test-agent", category="research", tag="db", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "DB Research"


def test_log_read_limit(tmp_path: Path) -> None:
    for i in range(5):
        _write_entry(tmp_path, title=f"Entry {i}")
    result = log_read(agent="test-agent", limit=2, workspace_root=tmp_path)
    assert len(result["entries"]) == 2
    assert result["total"] == 5


def test_log_read_not_found(tmp_path: Path) -> None:
    result = log_read(agent="nonexistent", workspace_root=tmp_path)
    assert result["error"] == "log_not_found"


def test_log_read_invalid_agent(tmp_path: Path) -> None:
    result = log_read(agent="Bad!", workspace_root=tmp_path)
    assert result["error"] == "invalid_agent"


def test_log_read_entries_have_ts_not_date(tmp_path: Path) -> None:
    _write_entry(tmp_path)
    result = log_read(agent="test-agent", workspace_root=tmp_path)
    entry = result["entries"][0]
    assert "ts" in entry
    assert entry["ts"].endswith("Z")
    assert "date" not in entry


# ---------------------------------------------------------------------------
# log_read — time filters
# ---------------------------------------------------------------------------


def test_log_read_since_relative(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _inject_entry(tmp_path, "test-agent", "L1", old_ts, title="Old entry")
    _inject_entry(tmp_path, "test-agent", "L2", recent_ts, title="Recent entry")
    result = log_read(agent="test-agent", since="1h", workspace_root=tmp_path)
    titles = [e["title"] for e in result["entries"]]
    assert "Recent entry" in titles
    assert "Old entry" not in titles


def test_log_read_until_relative(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _inject_entry(tmp_path, "test-agent", "L1", old_ts, title="Old entry")
    _inject_entry(tmp_path, "test-agent", "L2", recent_ts, title="Recent entry")
    result = log_read(agent="test-agent", until="1h", workspace_root=tmp_path)
    titles = [e["title"] for e in result["entries"]]
    assert "Old entry" in titles
    assert "Recent entry" not in titles


def test_log_read_since_iso_timestamp(tmp_path: Path) -> None:
    _inject_entry(tmp_path, "test-agent", "L1", "2026-01-01T00:00:00Z", title="Before")
    _inject_entry(tmp_path, "test-agent", "L2", "2026-06-01T00:00:00Z", title="After")
    result = log_read(agent="test-agent", since="2026-03-01T00:00:00Z", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "After"


def test_log_read_invalid_time_filter(tmp_path: Path) -> None:
    _write_entry(tmp_path)
    result = log_read(agent="test-agent", since="yesterday", workspace_root=tmp_path)
    assert result["error"] == "invalid_time_filter"


# ---------------------------------------------------------------------------
# log_read wildcard ("*")
# ---------------------------------------------------------------------------


def test_log_read_wildcard_merges_all_agents(tmp_path: Path) -> None:
    _write_entry(tmp_path, agent="agent-alpha", title="Alpha entry")
    _write_entry(tmp_path, agent="agent-beta", title="Beta entry")
    result = log_read(agent="*", workspace_root=tmp_path)
    assert result["agent"] == "*"
    titles = [e["title"] for e in result["entries"]]
    assert "Alpha entry" in titles
    assert "Beta entry" in titles
    assert result["total"] == 2


def test_log_read_wildcard_entries_include_agent_field(tmp_path: Path) -> None:
    _write_entry(tmp_path, agent="agent-alpha", title="Alpha entry")
    _write_entry(tmp_path, agent="agent-beta", title="Beta entry")
    result = log_read(agent="*", workspace_root=tmp_path)
    for entry in result["entries"]:
        assert "agent" in entry
    agents_in_entries = {e["agent"] for e in result["entries"]}
    assert agents_in_entries == {"agent-alpha", "agent-beta"}


def test_log_read_wildcard_filter_by_category(tmp_path: Path) -> None:
    _write_entry(tmp_path, agent="agent-alpha", title="Alpha research", category="research")
    _write_entry(tmp_path, agent="agent-beta", title="Beta decision", category="decision")
    result = log_read(agent="*", category="decision", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "Beta decision"


def test_log_read_wildcard_filter_by_tag(tmp_path: Path) -> None:
    _write_entry(tmp_path, agent="agent-alpha", title="Tagged", tags=["db"])
    _write_entry(tmp_path, agent="agent-beta", title="Untagged")
    result = log_read(agent="*", tag="db", workspace_root=tmp_path)
    assert result["total"] == 1
    assert result["entries"][0]["title"] == "Tagged"


def test_log_read_wildcard_no_logs_dir(tmp_path: Path) -> None:
    result = log_read(agent="*", workspace_root=tmp_path)
    assert result["agent"] == "*"
    assert result["entries"] == []
    assert result["total"] == 0


def test_log_read_wildcard_since_filter(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_ts = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _inject_entry(tmp_path, "agent-alpha", "L1", old_ts, title="Old alpha")
    _inject_entry(tmp_path, "agent-beta", "L1", recent_ts, title="Recent beta")
    result = log_read(agent="*", since="1h", workspace_root=tmp_path)
    titles = [e["title"] for e in result["entries"]]
    assert "Recent beta" in titles
    assert "Old alpha" not in titles


def test_log_read_wildcard_respects_limit(tmp_path: Path) -> None:
    for i in range(5):
        _write_entry(tmp_path, agent="agent-alpha", title=f"Alpha {i}")
    for i in range(5):
        _write_entry(tmp_path, agent="agent-beta", title=f"Beta {i}")
    result = log_read(agent="*", limit=3, workspace_root=tmp_path)
    assert len(result["entries"]) == 3
    assert result["total"] == 10
