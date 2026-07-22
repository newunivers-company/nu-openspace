from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openspace.runtime.execution_journal import (
    ExecutionAlreadyRunning,
    ExecutionJournal,
    ExecutionJournalError,
)


def test_journal_state_machine_and_idempotent_result_replay(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db")
    try:
        started = journal.start(task_id="task-1", prompt="do work")
        assert started.disposition == "started"
        journal.transition("task-1", "running")
        journal.bind_session("task-1", "session-1")
        assert journal.get("task-1")["session_id"] == "session-1"
        journal.transition("task-1", "finalizing")
        journal.finalize(
            "task-1",
            state="completed",
            result={"task_id": "task-1", "status": "success", "response": "done"},
            evidence_manifest_id="sha256:abc",
        )
        replayed = journal.start(task_id="task-1", prompt="do work")
        assert replayed.disposition == "replayed"
        assert replayed.result["response"] == "done"
        assert journal.metrics()["by_state"]["completed"] == 1
    finally:
        journal.close()


def test_journal_rejects_concurrent_or_conflicting_idempotency_use(tmp_path: Path) -> None:
    first = ExecutionJournal(tmp_path / "journal.db", lease_seconds=300)
    second = ExecutionJournal(tmp_path / "journal.db", lease_seconds=300)
    try:
        first.start(task_id="task-1", prompt="same", idempotency_key="request-1")
        with pytest.raises(ExecutionAlreadyRunning):
            second.start(task_id="task-1", prompt="same", idempotency_key="request-1")
        with pytest.raises(ExecutionJournalError, match="different prompt"):
            second.start(task_id="task-2", prompt="different", idempotency_key="request-1")
        with pytest.raises(ExecutionJournalError, match="different idempotency"):
            second.start(task_id="task-1", prompt="same", idempotency_key="request-2")
    finally:
        first.close()
        second.close()


def test_journal_recovers_expired_lease_and_allows_retry(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db", lease_seconds=1)
    try:
        journal.start(task_id="task-1", prompt="work")
        recovered = journal.recover_stale(
            now=datetime.now(timezone.utc) + timedelta(seconds=2)
        )
        assert recovered == 1
        assert journal.get("task-1")["state"] == "interrupted"
        retry = journal.start(task_id="task-1", prompt="work")
        assert retry.attempt == 2
        assert retry.state == "accepted"
    finally:
        journal.close()


def test_retry_keeps_canonical_task_id_for_event_integrity(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db", lease_seconds=1)
    try:
        journal.start(
            task_id="task-original",
            prompt="work",
            idempotency_key="request-1",
        )
        journal.recover_stale(now=datetime.now(timezone.utc) + timedelta(seconds=2))
        retry = journal.start(
            task_id="task-retry",
            prompt="work",
            idempotency_key="request-1",
        )
        assert retry.task_id == "task-original"
        assert journal.get("task-original")["attempt"] == 2
        assert journal.get("task-retry") is None
    finally:
        journal.close()


def test_only_current_lease_owner_can_mutate_active_execution(tmp_path: Path) -> None:
    owner = ExecutionJournal(tmp_path / "journal.db", lease_seconds=300)
    observer = ExecutionJournal(tmp_path / "journal.db", lease_seconds=300)
    try:
        owner.start(task_id="task-1", prompt="work")
        with pytest.raises(ExecutionJournalError, match="another runtime"):
            observer.transition("task-1", "running")
        with pytest.raises(ExecutionJournalError, match="inactive"):
            observer.heartbeat("task-1")
        owner.transition("task-1", "running")
        owner.heartbeat("task-1")
    finally:
        owner.close()
        observer.close()


def test_conflicting_task_and_idempotency_identifiers_are_rejected(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db")
    try:
        journal.start(task_id="task-1", prompt="one", idempotency_key="request-1")
        journal.transition("task-1", "failed")
        journal.start(task_id="task-2", prompt="two", idempotency_key="request-2")
        journal.transition("task-2", "failed")
        with pytest.raises(ExecutionJournalError, match="different executions"):
            journal.start(task_id="task-1", prompt="one", idempotency_key="request-2")
    finally:
        journal.close()


def test_journal_rejects_invalid_transition(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db")
    try:
        journal.start(task_id="task-1", prompt="work")
        with pytest.raises(ExecutionJournalError, match="invalid"):
            journal.transition("task-1", "completed")
    finally:
        journal.close()


def test_journal_rejects_non_finite_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="finite"):
        ExecutionJournal(tmp_path / "journal.db", lease_seconds=float("inf"))
