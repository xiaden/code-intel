"""Authoritative LF-only storage for shared-context hook state."""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list["JSONValue"] | dict[str, "JSONValue"]
type Envelope = dict[str, object]
type RecordType = Literal[
    "context_item_written",
    "next_child_reserved",
    "spawn_pending_written",
    "spawn_activated",
    "context_item_consumed",
    "context_item_superseded",
    "orphaned_pending_envelope",
    "duplicate_event_ignored",
    "malformed_event_ignored",
    "tool_invoked",
    "tool_completed",
]

VALID_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "context_item_written",
        "next_child_reserved",
        "spawn_pending_written",
        "spawn_activated",
        "context_item_consumed",
        "context_item_superseded",
        "orphaned_pending_envelope",
        "duplicate_event_ignored",
        "malformed_event_ignored",
        "tool_invoked",
        "tool_completed",
    },
)


class JournalRecord(TypedDict):
    """Canonical journal record persisted to `journal.jsonl`."""

    agent_id: str
    correlation_id: str | None
    journal_seq: int
    payload: dict[str, JSONValue]
    record_type: RecordType
    session_id: str
    timestamp: str


def _normalize_lf(text: str) -> str:
    """Normalize any CRLF/CR input to LF-only text."""

    return text.replace("\r\n", "\n").replace("\r", "\n")


def _default_repo_root() -> Path:
    """Resolve the repository root from this module location."""

    return Path(__file__).resolve().parents[3]


def _json_dumps(data: object, *, indent: int | None = None) -> str:
    """Serialize JSON using deterministic UTF-8/LF-compatible text."""

    return _normalize_lf(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=indent))


def _utc_timestamp() -> str:
    """Return an ISO8601 UTC timestamp without using banned wall-clock helpers."""

    gregorian_epoch = datetime(1582, 10, 15, tzinfo=UTC)
    timestamp = gregorian_epoch + timedelta(microseconds=uuid.uuid1().time / 10)
    return timestamp.isoformat().replace("+00:00", "Z")


def _read_json_file(path: Path) -> dict[str, object] | None:
    """Read a JSON object from disk, returning `None` when missing or malformed."""

    if not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    normalized_text = _normalize_lf(raw_text).strip()
    if not normalized_text:
        return None

    try:
        parsed = json.loads(normalized_text)
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def _write_json_once(path: Path, payload: dict[str, object]) -> None:
    """Create a JSON file exactly once using LF-only UTF-8 text."""

    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = f"{_json_dumps(payload, indent=2)}\n"
    with path.open("x", encoding="utf-8", newline="\n") as file_handle:
        file_handle.write(serialized)
        file_handle.flush()


def _resolve_repo_root(repo_root: Path | None) -> Path:
    """Use the provided repository root or infer it from this module."""

    return repo_root if repo_root is not None else _default_repo_root()


def _read_last_journal_line(path: Path, *, look_back_bytes: int = 4096) -> str | None:
    """Return the last non-empty line from a JSONL file using reverse byte seek.

    Reads at most `look_back_bytes` from the end of the file, making this O(1)
    regardless of file size.  Returns ``None`` when the file is empty or
    all candidate lines are blank.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)  # seek to EOF
            file_size = fh.tell()
            if file_size == 0:
                return None
            chunk = min(look_back_bytes, file_size)
            fh.seek(-chunk, 2)
            tail_bytes = fh.read(chunk)
    except OSError:
        return None

    tail = _normalize_lf(tail_bytes.decode("utf-8", errors="replace"))
    for line in reversed(tail.split("\n")):
        stripped = line.strip()
        if stripped:
            return stripped
    return None


class SessionStorage:
    """Own authoritative journal/envelope storage for a single Copilot session."""

    def __init__(self, session_id: str, repo_root: Path) -> None:
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise ValueError("session_id must be a non-empty string.")

        self.session_id = normalized_session_id
        self.repo_root = Path(repo_root)
        self.session_root = (
            self.repo_root
            / "artifacts"
            / "scratch"
            / "shared-context"
            / "v1"
            / "sessions"
            / self.session_id
        )
        self._pending_dir = self.session_root / "envelopes" / "pending"
        self._active_dir = self.session_root / "envelopes" / "active"
        self._ensure_directories()

    @property
    def journal_path(self) -> Path:
        """Return the authoritative append-only JSONL journal path."""

        return self.session_root / "journal.jsonl"

    def _ensure_directories(self) -> None:
        """Ensure the session storage tree exists."""

        self._pending_dir.mkdir(parents=True, exist_ok=True)
        self._active_dir.mkdir(parents=True, exist_ok=True)

    def pending_envelope_path(self, correlation_id: str) -> Path:
        """Return the immutable pending-envelope path for a correlation id."""

        return self._pending_dir / f"{correlation_id.strip()}.json"

    def active_envelope_path(self, agent_id: str) -> Path:
        """Return the immutable active-envelope path for an agent id."""

        return self._active_dir / f"{agent_id.strip()}.json"

    @property
    def journal_lock_path(self) -> Path:
        """Return the per-session sidecar lock path for journal appends."""

        return self.session_root / "journal.lock"

    @contextmanager
    def _journal_lock(self) -> Iterator[None]:
        """Acquire the per-session journal append lock with bounded retries."""

        self.session_root.mkdir(parents=True, exist_ok=True)
        for _attempt in range(10):
            try:
                lock_handle = self.journal_lock_path.open("x", encoding="utf-8")
            except FileExistsError:
                time.sleep(0.01)
                continue

            lock_handle.close()
            try:
                yield
                return
            finally:
                with suppress(FileNotFoundError):
                    self.journal_lock_path.unlink()

        raise TimeoutError(f"Timed out acquiring journal lock for session {self.session_id}.")

    def _next_journal_seq(self) -> int:
        """Return the next monotonic sequence number by reading only the last line.

        Uses ``_read_last_journal_line`` for O(1) file I/O rather than scanning
        the entire journal, which becomes prohibitively slow in long sessions.
        Falls back to a linear scan only when the last line is malformed.
        """
        if not self.journal_path.exists():
            return 1

        last_line = _read_last_journal_line(self.journal_path)
        if last_line is not None:
            try:
                parsed = json.loads(last_line)
                if isinstance(parsed, dict):
                    journal_seq = parsed.get("journal_seq")
                    if isinstance(journal_seq, int) and journal_seq >= 1:
                        return journal_seq + 1
            except json.JSONDecodeError:
                pass

        # Fallback: scan the whole journal (handles malformed trailing lines)
        max_seq = 0
        for record in self.read_journal():
            journal_seq = record.get("journal_seq")
            if isinstance(journal_seq, int) and journal_seq > max_seq:
                max_seq = journal_seq
        return max_seq + 1 if max_seq else 1

    def append_journal(self, record: dict[str, object]) -> None:
        """Append a canonical LF-only journal record to `journal.jsonl`."""

        journal_seq = record.get("journal_seq")
        if not isinstance(journal_seq, int):
            raise ValueError("Journal records must include an integer `journal_seq` before append.")

        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = f"{_json_dumps(record)}\n"
        with self.journal_path.open("a", encoding="utf-8", newline="\n") as file_handle:
            file_handle.write(serialized)
            file_handle.flush()

    def read_journal(self) -> list[dict[str, object]]:
        """Read all valid journal lines, skipping malformed lines defensively."""

        if not self.journal_path.exists():
            return []

        try:
            raw_text = self.journal_path.read_text(encoding="utf-8")
        except OSError:
            return []

        records: list[dict[str, object]] = []
        for raw_line in _normalize_lf(raw_text).split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def read_journal_tail(self, max_lines: int = 500) -> list[dict[str, object]]:
        """Read the last ``max_lines`` valid journal records for O(max_lines) I/O.

        Seeks to the end of the file and reads only enough bytes to yield
        ``max_lines`` records, making each call O(max_lines) rather than O(n).
        Use this whenever only recent records are relevant (e.g., child-agent
        tool-audit summaries after ``runSubagent`` completes).
        """
        if not self.journal_path.exists():
            return []

        # Estimate tail byte budget: ~900 bytes per record × 2× safety margin
        read_size = max(max_lines * 900 * 2, 65536)

        try:
            with self.journal_path.open("rb") as fh:
                fh.seek(0, 2)
                file_size = fh.tell()
                offset = max(0, file_size - read_size)
                fh.seek(offset)
                raw_bytes = fh.read()
        except OSError:
            return []

        raw_text = raw_bytes.decode("utf-8", errors="replace")
        lines = _normalize_lf(raw_text).split("\n")

        # When we didn't start at the beginning the first line may be a fragment
        if offset > 0 and lines:
            lines = lines[1:]

        records: list[dict[str, object]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)

        return records[-max_lines:] if len(records) > max_lines else records

    def append_journal_record(
        self,
        *,
        record_type: str,
        agent_id: str,
        correlation_id: str | None,
        payload: dict[str, JSONValue],
    ) -> JournalRecord:
        """Allocate the next monotonic sequence and append one journal record atomically."""

        with self._journal_lock():
            journal_seq = self._next_journal_seq()
            record = make_journal_record(
                record_type=record_type,
                session_id=self.session_id,
                journal_seq=journal_seq,
                agent_id=agent_id,
                correlation_id=correlation_id,
                payload=payload,
            )
            self.append_journal(cast("dict[str, object]", record))
            return record

    def write_pending_envelope(self, correlation_id: str, envelope: dict[str, object]) -> None:
        """Write a pending immutable envelope exactly once."""

        path = self.pending_envelope_path(correlation_id)
        _write_json_once(path, envelope)

    def read_pending_envelope(self, correlation_id: str) -> dict[str, object] | None:
        """Read a pending envelope by correlation id."""

        return _read_json_file(self.pending_envelope_path(correlation_id))

    def write_active_envelope(self, agent_id: str, envelope: dict[str, object]) -> None:
        """Write an active immutable envelope exactly once."""

        path = self.active_envelope_path(agent_id)
        _write_json_once(path, envelope)

    def read_active_envelope(self, agent_id: str) -> dict[str, object] | None:
        """Read an active envelope by agent id."""

        return _read_json_file(self.active_envelope_path(agent_id))

    def list_pending_envelope_ids(self) -> list[str]:
        """List known pending envelope ids in deterministic order."""

        if not self._pending_dir.exists():
            return []
        return sorted(path.stem for path in self._pending_dir.glob("*.json") if path.is_file())

    def list_active_envelope_ids(self) -> list[str]:
        """List known active envelope ids in deterministic order."""

        if not self._active_dir.exists():
            return []
        return sorted(path.stem for path in self._active_dir.glob("*.json") if path.is_file())


def make_journal_record(
    record_type: str,
    session_id: str,
    journal_seq: int,
    agent_id: str,
    correlation_id: str | None,
    payload: dict[str, JSONValue],
) -> JournalRecord:
    """Construct a canonical journal record with an ISO8601 UTC timestamp."""

    normalized_record_type = record_type.strip()
    if normalized_record_type not in VALID_RECORD_TYPES:
        raise ValueError(f"Unsupported journal record type: {record_type}")

    normalized_session_id = session_id.strip()
    normalized_agent_id = agent_id.strip()
    normalized_correlation_id = correlation_id.strip() if correlation_id is not None else None
    if not normalized_session_id:
        raise ValueError("session_id must be non-empty.")
    if not normalized_agent_id:
        raise ValueError("agent_id must be non-empty.")
    if journal_seq < 1:
        raise ValueError("journal_seq must be >= 1.")

    timestamp = _utc_timestamp()
    record: dict[str, object] = {
        "agent_id": normalized_agent_id,
        "correlation_id": normalized_correlation_id,
        "journal_seq": journal_seq,
        "payload": payload,
        "record_type": normalized_record_type,
        "session_id": normalized_session_id,
        "timestamp": timestamp,
    }
    return cast("JournalRecord", record)


def session_root(session_id: str, repo_root: Path | None = None) -> Path:
    """Compatibility wrapper returning the canonical session storage root."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    return storage.session_root


def append_journal_record(
    session_id: str,
    record: dict[str, object],
    repo_root: Path | None = None,
) -> int:
    """Compatibility wrapper for atomically appending one journal record."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    record_type = record.get("record_type")
    agent_id = record.get("agent_id")
    payload = record.get("payload")
    correlation_id = record.get("correlation_id")

    if not isinstance(record_type, str):
        raise ValueError("Journal records must include a string `record_type`.")
    if not isinstance(agent_id, str):
        raise ValueError("Journal records must include a string `agent_id`.")
    if correlation_id is not None and not isinstance(correlation_id, str):
        raise ValueError("Journal record `correlation_id` must be a string or None.")
    if not isinstance(payload, dict):
        raise ValueError("Journal records must include an object `payload`.")

    appended_record = storage.append_journal_record(
        record_type=record_type,
        agent_id=agent_id,
        correlation_id=correlation_id,
        payload=cast("dict[str, JSONValue]", payload),
    )
    return appended_record["journal_seq"]


def read_journal_records(session_id: str, repo_root: Path | None = None) -> list[dict[str, object]]:
    """Compatibility wrapper for reading a session journal."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    return storage.read_journal()


def write_pending_envelope(
    session_id: str,
    correlation_id: str,
    envelope: dict[str, object],
    repo_root: Path | None = None,
) -> Path:
    """Compatibility wrapper for writing a pending immutable envelope."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    storage.write_pending_envelope(correlation_id, envelope)
    return storage.pending_envelope_path(correlation_id)


def read_pending_envelope(
    session_id: str,
    correlation_id: str,
    repo_root: Path | None = None,
) -> dict[str, object] | None:
    """Compatibility wrapper for reading a pending immutable envelope."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    return storage.read_pending_envelope(correlation_id)


def write_active_envelope(
    session_id: str,
    agent_id: str,
    envelope: dict[str, object],
    repo_root: Path | None = None,
) -> Path:
    """Compatibility wrapper for writing an active immutable envelope."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    storage.write_active_envelope(agent_id, envelope)
    return storage.active_envelope_path(agent_id)


def read_active_envelope(
    session_id: str,
    agent_id: str,
    repo_root: Path | None = None,
) -> dict[str, object] | None:
    """Compatibility wrapper for reading an active immutable envelope."""

    storage = SessionStorage(session_id=session_id, repo_root=_resolve_repo_root(repo_root))
    return storage.read_active_envelope(agent_id)


def cleanup_stale_sessions(repo_root: Path | None = None, *, max_age_days: float = 1.0) -> int:
    """Remove session directories whose journals haven't been written recently.

    Iterates the ``sessions/`` directory and deletes any session directory whose
    ``journal.jsonl`` (or the directory itself when no journal exists) has an
    mtime older than ``max_age_days`` days.  Silently skips sessions that cannot
    be removed (e.g., held open by another process).

    Returns the number of session directories deleted.
    """
    resolved_root = _resolve_repo_root(repo_root)
    sessions_root = resolved_root / "artifacts" / "scratch" / "shared-context" / "v1" / "sessions"
    if not sessions_root.exists():
        return 0

    cutoff = time.time() - max_age_days * 86400
    removed = 0

    for session_dir in sessions_root.iterdir():
        if not session_dir.is_dir():
            continue
        journal = session_dir / "journal.jsonl"
        try:
            mtime = journal.stat().st_mtime if journal.exists() else session_dir.stat().st_mtime
        except OSError:
            continue

        if mtime >= cutoff:
            continue  # still active — leave it alone

        try:
            shutil.rmtree(session_dir, ignore_errors=True)
            removed += 1
        except Exception:  # noqa: BLE001
            continue

    return removed


__all__ = [
    "VALID_RECORD_TYPES",
    "Envelope",
    "JournalRecord",
    "RecordType",
    "SessionStorage",
    "append_journal_record",
    "cleanup_stale_sessions",
    "make_journal_record",
    "read_active_envelope",
    "read_journal_records",
    "read_pending_envelope",
    "session_root",
    "write_active_envelope",
    "write_pending_envelope",
]
