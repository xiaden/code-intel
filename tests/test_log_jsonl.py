"""Tests for log_jsonl helper — JSONL log parsing, appending, and ID management.

Covers:
- validate_category: each valid category, invalid
- validate_agent_name: valid names, empty, single char, uppercase, leading/trailing hyphens
- parse_time_filter: relative durations, ISO timestamps, empty, invalid
- next_entry_id: no file, empty file, existing entries
- append_entry + read_entries: round-trip
- read_entries: skips malformed lines
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcp_code_intel.helpers.log_jsonl import (
    CATEGORIES,
    LogEntry,
    append_entry,
    next_entry_id,
    parse_time_filter,
    read_entries,
    ts_to_datetime,
    validate_agent_name,
    validate_category,
)

# ---------------------------------------------------------------------------
# validate_category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", sorted(CATEGORIES))
def test_validate_category_valid(category: str) -> None:
    assert validate_category(category) is None


def test_validate_category_invalid() -> None:
    err = validate_category("invalid-cat")
    assert err is not None


def test_validate_category_empty() -> None:
    err = validate_category("")
    assert err is not None


def test_validate_category_uppercase() -> None:
    err = validate_category("Research")
    assert err is not None


# ---------------------------------------------------------------------------
# validate_agent_name
# ---------------------------------------------------------------------------


def test_validate_agent_name_valid() -> None:
    assert validate_agent_name("rnd-ddauthor") is None


def test_validate_agent_name_valid_short() -> None:
    assert validate_agent_name("ab") is None


def test_validate_agent_name_empty() -> None:
    err = validate_agent_name("")
    assert err is not None
    assert "empty" in err.lower()


def test_validate_agent_name_single_char() -> None:
    err = validate_agent_name("a")
    assert err is not None


def test_validate_agent_name_uppercase() -> None:
    err = validate_agent_name("RND-Author")
    assert err is not None


def test_validate_agent_name_leading_hyphen() -> None:
    err = validate_agent_name("-my-agent")
    assert err is not None


def test_validate_agent_name_trailing_hyphen() -> None:
    err = validate_agent_name("my-agent-")
    assert err is not None


def test_validate_agent_name_with_numbers() -> None:
    assert validate_agent_name("agent42") is None


# ---------------------------------------------------------------------------
# parse_time_filter
# ---------------------------------------------------------------------------


def test_parse_time_filter_empty_returns_none() -> None:
    assert parse_time_filter("") is None


def test_parse_time_filter_minutes() -> None:
    before = datetime.now(UTC)
    result = parse_time_filter("30m")
    after = datetime.now(UTC)
    assert result is not None
    expected_low = before - timedelta(minutes=30)
    expected_high = after - timedelta(minutes=30) + timedelta(seconds=1)
    assert expected_low <= result <= expected_high


def test_parse_time_filter_hours() -> None:
    result = parse_time_filter("2h")
    assert result is not None
    delta = datetime.now(UTC) - result
    assert timedelta(hours=1, minutes=59) < delta < timedelta(hours=2, minutes=1)


def test_parse_time_filter_days() -> None:
    result = parse_time_filter("7d")
    assert result is not None
    delta = datetime.now(UTC) - result
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_parse_time_filter_iso_with_z() -> None:
    result = parse_time_filter("2026-05-28T10:00:00Z")
    assert result == datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC)


def test_parse_time_filter_iso_without_z() -> None:
    result = parse_time_filter("2026-05-28T10:00:00")
    assert result == datetime(2026, 5, 28, 10, 0, 0, tzinfo=UTC)


def test_parse_time_filter_invalid_raises() -> None:
    with pytest.raises(ValueError, match="Unrecognised"):
        parse_time_filter("yesterday")


# ---------------------------------------------------------------------------
# ts_to_datetime
# ---------------------------------------------------------------------------


def test_ts_to_datetime_with_z() -> None:
    dt = ts_to_datetime("2026-05-28T11:45:47Z")
    assert dt == datetime(2026, 5, 28, 11, 45, 47, tzinfo=UTC)


# ---------------------------------------------------------------------------
# next_entry_id
# ---------------------------------------------------------------------------


def test_next_entry_id_no_file(tmp_path: Path) -> None:
    assert next_entry_id(tmp_path / "nonexistent.log.jsonl") == "L1"


def test_next_entry_id_empty_file(tmp_path: Path) -> None:
    f = tmp_path / "a.log.jsonl"
    f.write_text("", encoding="utf-8")
    assert next_entry_id(f) == "L1"


def test_next_entry_id_with_entries(tmp_path: Path) -> None:
    f = tmp_path / "a.log.jsonl"
    f.write_text(
        json.dumps({"id": "L1", "ts": "2026-01-01T00:00:00Z"})
        + "\n"
        + json.dumps({"id": "L3", "ts": "2026-01-03T00:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    assert next_entry_id(f) == "L4"


def test_next_entry_id_skips_malformed(tmp_path: Path) -> None:
    f = tmp_path / "a.log.jsonl"
    f.write_text(
        json.dumps({"id": "L2", "ts": "2026-01-02T00:00:00Z"}) + "\n" + "not-json\n",
        encoding="utf-8",
    )
    assert next_entry_id(f) == "L3"


# ---------------------------------------------------------------------------
# append_entry + read_entries
# ---------------------------------------------------------------------------


def _make_entry(n: int, category: str = "research", tags: list[str] | None = None) -> LogEntry:
    return LogEntry(
        id=f"L{n}",
        ts=f"2026-01-{n:02d}T10:00:00Z",
        category=category,
        title=f"Entry {n}",
        tags=tags or [],
        body=f"Body {n}",
    )


def test_append_and_read_single(tmp_path: Path) -> None:
    f = tmp_path / "t.log.jsonl"
    entry = _make_entry(1)
    append_entry(f, entry)
    entries = read_entries(f)
    assert len(entries) == 1
    assert entries[0].id == "L1"
    assert entries[0].title == "Entry 1"
    assert entries[0].category == "research"
    assert entries[0].body == "Body 1"
    assert entries[0].ts == "2026-01-01T10:00:00Z"


def test_append_and_read_multiple(tmp_path: Path) -> None:
    f = tmp_path / "t.log.jsonl"
    for i in range(1, 4):
        append_entry(f, _make_entry(i))
    entries = read_entries(f)
    assert len(entries) == 3
    assert [e.id for e in entries] == ["L1", "L2", "L3"]


def test_read_entries_skips_malformed_lines(tmp_path: Path) -> None:
    f = tmp_path / "t.log.jsonl"
    f.write_text(
        json.dumps(
            {
                "id": "L1",
                "ts": "2026-01-01T00:00:00Z",
                "category": "research",
                "title": "Good",
                "tags": [],
                "body": "",
            }
        )
        + "\n"
        + "this is not json\n"
        + json.dumps(
            {
                "id": "L2",
                "ts": "2026-01-02T00:00:00Z",
                "category": "decision",
                "title": "Also good",
                "tags": [],
                "body": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries = read_entries(f)
    assert len(entries) == 2
    assert entries[0].id == "L1"
    assert entries[1].id == "L2"


def test_append_creates_parent_dirs(tmp_path: Path) -> None:
    f = tmp_path / "nested" / "dir" / "a.log.jsonl"
    append_entry(f, _make_entry(1))
    assert f.exists()


def test_round_trip_unicode(tmp_path: Path) -> None:
    f = tmp_path / "t.log.jsonl"
    entry = LogEntry(
        id="L1",
        ts="2026-01-01T00:00:00Z",
        category="research",
        title="Unicode: \u00e9\u00e0\u00fc",
        tags=["\u6e2c\u8a66"],
        body="Body with \u2603",
    )
    append_entry(f, entry)
    entries = read_entries(f)
    assert entries[0].title == "Unicode: \u00e9\u00e0\u00fc"
    assert entries[0].tags == ["\u6e2c\u8a66"]
    assert "\u2603" in entries[0].body
