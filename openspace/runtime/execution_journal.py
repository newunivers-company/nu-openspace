"""Durable, idempotent execution journal for the unified runtime."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
ACTIVE_STATES = frozenset({"accepted", "running", "finalizing"})
_ALLOWED_TRANSITIONS = {
    "accepted": {"running", "failed", "cancelled", "interrupted"},
    "running": {"finalizing", "failed", "cancelled", "interrupted"},
    "finalizing": {"completed", "failed", "cancelled", "interrupted"},
    "failed": {"accepted"},
    "cancelled": {"accepted"},
    "interrupted": {"accepted"},
    "completed": set(),
}


class ExecutionJournalError(RuntimeError):
    """Base error for durable execution-journal failures."""


class ExecutionAlreadyRunning(ExecutionJournalError):
    """Raised when an unexpired execution owns the same idempotency key."""


@dataclass(frozen=True, slots=True)
class JournalStart:
    disposition: str
    task_id: str
    state: str
    attempt: int
    result: dict[str, Any] | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


class ExecutionJournal:
    """SQLite state machine with lease recovery and completed-result replay."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        lease_seconds: float = 900.0,
        max_result_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = float(lease_seconds)
        if not math.isfinite(self.lease_seconds):
            raise ValueError("lease_seconds must be finite")
        self.lease_seconds = max(1.0, self.lease_seconds)
        self.max_result_bytes = max(1024, int(max_result_bytes))
        self.owner_id = f"runtime:{os.getpid()}:{uuid.uuid4().hex}"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._init_schema()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self.recover_stale()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runtime_executions (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                session_id TEXT NOT NULL DEFAULT '',
                prompt_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                owner_id TEXT NOT NULL,
                lease_expires_at TEXT,
                accepted_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                result_json TEXT,
                result_sha256 TEXT,
                error_code TEXT,
                evidence_manifest_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_executions_state_updated
                ON runtime_executions(state, updated_at);
            CREATE TABLE IF NOT EXISTS runtime_execution_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(task_id) REFERENCES runtime_executions(task_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_runtime_execution_events_task
                ON runtime_execution_events(task_id, id);
            """
        )

    def start(
        self,
        *,
        task_id: str,
        prompt: str,
        idempotency_key: str | None = None,
        session_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> JournalStart:
        normalized_task_id = str(task_id or "").strip()
        normalized_key = str(idempotency_key or normalized_task_id).strip()
        if not normalized_task_id or not normalized_key:
            raise ExecutionJournalError("task_id and idempotency_key are required")
        prompt_hash = hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()
        now = _utc_now()
        lease = now + timedelta(seconds=self.lease_seconds)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT * FROM runtime_executions WHERE task_id=? OR idempotency_key=?",
                    (normalized_task_id, normalized_key),
                ).fetchall()
                if len(rows) > 1:
                    raise ExecutionJournalError(
                        "task_id and idempotency_key reference different executions"
                    )
                row = rows[0] if rows else None
                if row is not None:
                    if row["idempotency_key"] != normalized_key:
                        raise ExecutionJournalError(
                            "task_id was already used with a different idempotency key"
                        )
                    if row["prompt_sha256"] != prompt_hash:
                        raise ExecutionJournalError(
                            "idempotency key was already used for a different prompt"
                        )
                    state = str(row["state"])
                    if state == "completed":
                        result = _json_object(row["result_json"])
                        if result is None:
                            raise ExecutionJournalError(
                                "completed execution has no replayable result"
                            )
                        self._conn.commit()
                        return JournalStart(
                            "replayed",
                            str(row["task_id"]),
                            state,
                            int(row["attempt"]),
                            result,
                        )
                    lease_expiry = _parse_time(row["lease_expires_at"])
                    if state in ACTIVE_STATES and lease_expiry and lease_expiry > now:
                        raise ExecutionAlreadyRunning(
                            f"execution already active for idempotency key: {normalized_key}"
                        )
                    previous = state
                    canonical_task_id = str(row["task_id"])
                    if state in ACTIVE_STATES:
                        self._event(
                            canonical_task_id,
                            state,
                            "interrupted",
                            now,
                            {"reason": "lease_expired_on_retry"},
                        )
                        previous = "interrupted"
                    attempt = int(row["attempt"] or 1) + 1
                    self._conn.execute(
                        """
                        UPDATE runtime_executions SET
                            session_id=?, state='accepted', attempt=?,
                            owner_id=?, lease_expires_at=?, accepted_at=?, started_at=NULL,
                            completed_at=NULL, updated_at=?, result_json=NULL,
                            result_sha256=NULL, error_code=NULL,
                            evidence_manifest_id=NULL, metadata_json=?
                        WHERE idempotency_key=?
                        """,
                        (
                            session_id,
                            attempt,
                            self.owner_id,
                            _iso(lease),
                            _iso(now),
                            _iso(now),
                            _safe_json(metadata or {}),
                            normalized_key,
                        ),
                    )
                    self._event(
                        canonical_task_id,
                        previous,
                        "accepted",
                        now,
                        {
                            "retry": True,
                            "requested_task_id": normalized_task_id,
                        },
                    )
                else:
                    canonical_task_id = normalized_task_id
                    attempt = 1
                    self._conn.execute(
                        """
                        INSERT INTO runtime_executions (
                            task_id, idempotency_key, session_id, prompt_sha256,
                            state, attempt, owner_id, lease_expires_at,
                            accepted_at, updated_at, metadata_json
                        ) VALUES (?, ?, ?, ?, 'accepted', 1, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_task_id,
                            normalized_key,
                            session_id,
                            prompt_hash,
                            self.owner_id,
                            _iso(lease),
                            _iso(now),
                            _iso(now),
                            _safe_json(metadata or {}),
                        ),
                    )
                    self._event(normalized_task_id, None, "accepted", now, {})
                self._conn.commit()
                return JournalStart("started", canonical_task_id, "accepted", attempt)
            except Exception:
                self._conn.rollback()
                raise

    def transition(
        self,
        task_id: str,
        to_state: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if to_state not in ACTIVE_STATES | TERMINAL_STATES:
            raise ExecutionJournalError(f"unknown execution state: {to_state}")
        now = _utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state, owner_id FROM runtime_executions WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise ExecutionJournalError(f"unknown task_id: {task_id}")
                previous = str(row["state"])
                if previous in ACTIVE_STATES and row["owner_id"] != self.owner_id:
                    raise ExecutionJournalError(
                        f"execution is owned by another runtime: {task_id}"
                    )
                if to_state == previous:
                    self._conn.commit()
                    return
                if to_state not in _ALLOWED_TRANSITIONS.get(previous, set()):
                    raise ExecutionJournalError(
                        f"invalid execution transition: {previous} -> {to_state}"
                    )
                lease = (
                    _iso(now + timedelta(seconds=self.lease_seconds))
                    if to_state in ACTIVE_STATES
                    else None
                )
                started_at = _iso(now) if to_state == "running" else None
                completed_at = _iso(now) if to_state in TERMINAL_STATES else None
                self._conn.execute(
                    """
                    UPDATE runtime_executions SET state=?, owner_id=?,
                        lease_expires_at=?, updated_at=?,
                        started_at=COALESCE(?, started_at),
                        completed_at=COALESCE(?, completed_at)
                    WHERE task_id=?
                    """,
                    (
                        to_state,
                        self.owner_id,
                        lease,
                        _iso(now),
                        started_at,
                        completed_at,
                        task_id,
                    ),
                )
                self._event(task_id, previous, to_state, now, metadata or {})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def heartbeat(self, task_id: str) -> None:
        now = _utc_now()
        with self._lock:
            changed = self._conn.execute(
                """
                UPDATE runtime_executions SET lease_expires_at=?, updated_at=?
                WHERE task_id=? AND owner_id=?
                  AND state IN ('accepted', 'running', 'finalizing')
                """,
                (
                    _iso(now + timedelta(seconds=self.lease_seconds)),
                    _iso(now),
                    task_id,
                    self.owner_id,
                ),
            ).rowcount
        if not changed:
            raise ExecutionJournalError(f"cannot heartbeat inactive task: {task_id}")

    def bind_session(self, task_id: str, session_id: str) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            raise ExecutionJournalError("session_id is required")
        now = _utc_now()
        with self._lock:
            changed = self._conn.execute(
                """
                UPDATE runtime_executions SET session_id=?, updated_at=?
                WHERE task_id=? AND owner_id=?
                  AND state IN ('accepted', 'running', 'finalizing')
                """,
                (
                    normalized_session_id,
                    _iso(now),
                    task_id,
                    self.owner_id,
                ),
            ).rowcount
        if not changed:
            raise ExecutionJournalError(f"cannot bind session to inactive task: {task_id}")

    def finalize(
        self,
        task_id: str,
        *,
        state: str,
        result: Mapping[str, Any],
        error_code: str | None = None,
        evidence_manifest_id: str | None = None,
    ) -> None:
        if state not in {"completed", "failed", "cancelled"}:
            raise ExecutionJournalError(f"invalid final state: {state}")
        payload = _safe_json(dict(result))
        encoded = payload.encode("utf-8")
        if len(encoded) > self.max_result_bytes:
            payload = _safe_json(
                {
                    "status": result.get("status", state),
                    "task_id": task_id,
                    "error": "journal_result_exceeded_limit",
                    "result_sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
        result_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state, owner_id FROM runtime_executions WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise ExecutionJournalError(f"unknown task_id: {task_id}")
                previous = str(row["state"])
                if previous in ACTIVE_STATES and row["owner_id"] != self.owner_id:
                    raise ExecutionJournalError(
                        f"execution is owned by another runtime: {task_id}"
                    )
                if state not in _ALLOWED_TRANSITIONS.get(previous, set()):
                    raise ExecutionJournalError(
                        f"invalid execution transition: {previous} -> {state}"
                    )
                self._conn.execute(
                    """
                    UPDATE runtime_executions SET state=?, lease_expires_at=NULL,
                        updated_at=?, completed_at=?, result_json=?, result_sha256=?,
                        error_code=?, evidence_manifest_id=? WHERE task_id=?
                    """,
                    (
                        state,
                        _iso(now),
                        _iso(now),
                        payload,
                        result_hash,
                        error_code,
                        evidence_manifest_id,
                        task_id,
                    ),
                )
                self._event(task_id, previous, state, now, {"error_code": error_code})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def recover_stale(self, *, now: datetime | None = None) -> int:
        reference = now or _utc_now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    """
                    SELECT task_id, state FROM runtime_executions
                    WHERE state IN ('accepted', 'running', 'finalizing')
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < ?
                    """,
                    (_iso(reference),),
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        """
                        UPDATE runtime_executions SET state='interrupted',
                            lease_expires_at=NULL, updated_at=?, completed_at=?,
                            error_code='STALE_EXECUTION_RECOVERED'
                        WHERE task_id=?
                        """,
                        (_iso(reference), _iso(reference), row["task_id"]),
                    )
                    self._event(
                        str(row["task_id"]),
                        str(row["state"]),
                        "interrupted",
                        reference,
                        {"reason": "lease_expired"},
                    )
                self._conn.commit()
                return len(rows)
            except Exception:
                self._conn.rollback()
                raise

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runtime_executions WHERE task_id=?",
                (task_id,),
            ).fetchone()
        return _row_dict(row) if row is not None else None

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS count FROM runtime_executions GROUP BY state"
            ).fetchall()
            total = self._conn.execute("SELECT COUNT(*) FROM runtime_executions").fetchone()[0]
        by_state = {str(row["state"]): int(row["count"]) for row in rows}
        return {
            "total": int(total),
            "active": sum(by_state.get(state, 0) for state in ACTIVE_STATES),
            "terminal": sum(by_state.get(state, 0) for state in TERMINAL_STATES),
            "by_state": by_state,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _event(
        self,
        task_id: str,
        from_state: str | None,
        to_state: str,
        occurred_at: datetime,
        metadata: Mapping[str, Any],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO runtime_execution_events (
                task_id, from_state, to_state, occurred_at, owner_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                from_state,
                to_state,
                _iso(occurred_at),
                self.owner_id,
                _safe_json(metadata),
            ),
        )


def _safe_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_object(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["metadata"] = _json_object(payload.pop("metadata_json", None)) or {}
    payload["result"] = _json_object(payload.pop("result_json", None))
    return payload
