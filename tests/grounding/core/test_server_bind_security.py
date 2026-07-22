from pathlib import Path
import json
import sqlite3

import pytest

from openspace.entrypoints.dashboard.server import (
    create_app,
    is_loopback_host,
    validate_dashboard_bind,
)
from openspace.entrypoints.mcp.server import validate_mcp_http_bind
from openspace.evidence import create_run_manifest
from openspace.runtime.execution_journal import ExecutionJournal


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_dashboard_loopback_hosts_are_accepted(host: str) -> None:
    assert is_loopback_host(host)
    validate_dashboard_bind(host, "")


def test_dashboard_remote_bind_requires_token() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        validate_dashboard_bind("0.0.0.0", "")

    validate_dashboard_bind("0.0.0.0", "long-random-token")
    with pytest.raises(ValueError, match="non-loopback"):
        validate_dashboard_bind("0.0.0.0", "", "[]")
    validate_dashboard_bind(
        "0.0.0.0",
        "",
        '{"viewer-secret-token":{"role":"viewer","subject":"analyst"}}',
    )


@pytest.mark.parametrize("transport", ["sse", "streamable-http"])
def test_mcp_http_transports_refuse_non_loopback(transport: str) -> None:
    validate_mcp_http_bind("127.0.0.1", transport)
    with pytest.raises(ValueError, match="non-loopback"):
        validate_mcp_http_bind("0.0.0.0", transport)


def test_mcp_stdio_does_not_have_a_network_bind() -> None:
    validate_mcp_http_bind("0.0.0.0", "stdio")


def test_dashboard_token_protects_api_and_static_routes(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "skills.db",
        evidence_db_path=tmp_path / "evidence.db",
        auth_token="test-secret-token",
    )
    app.testing = True
    client = app.test_client()

    unauthorized = client.get("/api/v1/health")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["WWW-Authenticate"] == "Bearer"

    bearer = client.get(
        "/api/v1/health",
        headers={"Authorization": "Bearer test-secret-token"},
    )
    assert bearer.status_code == 200

    rejected = client.post("/auth", data={"token": "wrong"})
    assert rejected.status_code == 401
    signed_in = client.post("/auth", data={"token": "test-secret-token"})
    assert signed_in.status_code == 302
    assert "test-secret-token" not in signed_in.headers["Set-Cookie"]
    assert client.get("/api/v1/health").status_code == 200


def test_dashboard_role_tokens_enforce_viewer_operator_boundary(tmp_path: Path) -> None:
    audit_path = tmp_path / "dashboard-audit.db"
    tokens = json.dumps(
        {
            "viewer-secret-token": {"role": "viewer", "subject": "analyst"},
            "operator-secret-token": {"role": "operator", "subject": "ops"},
        }
    )
    app = create_app(
        db_path=tmp_path / "skills.db",
        evidence_db_path=tmp_path / "evidence.db",
        auth_token="",
        auth_tokens_json=tokens,
        auth_audit_path=audit_path,
    )
    app.testing = True
    client = app.test_client()
    viewer_headers = {"Authorization": "Bearer viewer-secret-token"}
    operator_headers = {"Authorization": "Bearer operator-secret-token"}

    whoami = client.get("/api/v1/auth/whoami", headers=viewer_headers)
    assert whoami.status_code == 200
    assert whoami.get_json()["role"] == "viewer"
    assert "viewer-secret-token" not in json.dumps(whoami.get_json())

    denied = client.post(
        "/api/v1/evolution/candidates/missing/reject",
        headers=viewer_headers,
        json={"reason": "test"},
    )
    assert denied.status_code == 403
    authorized = client.post(
        "/api/v1/evolution/candidates/missing/reject",
        headers=operator_headers,
        json={"reason": "test"},
    )
    assert authorized.status_code == 404

    with sqlite3.connect(audit_path) as connection:
        rows = connection.execute(
            "SELECT subject, role, outcome, details_json FROM dashboard_audit_events"
        ).fetchall()
    assert ("analyst", "viewer", "denied", '{"required_role":"operator"}') in rows
    assert any(row[:3] == ("ops", "operator", "failed") for row in rows)
    assert all("viewer-secret-token" not in str(row) for row in rows)


def test_dashboard_observability_reports_evidence_and_execution_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence_root = tmp_path / "session"
    evidence_root.mkdir()
    (evidence_root / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    create_run_manifest(
        evidence_root,
        run={"task_id": "task-1", "session_id": "session-1", "status": "completed"},
    )
    journal_path = tmp_path / "execution-journal.db"
    journal = ExecutionJournal(journal_path)
    journal.start(task_id="task-1", prompt="work")
    journal.transition("task-1", "running")
    journal.transition("task-1", "finalizing")
    journal.finalize(
        "task-1",
        state="completed",
        result={"task_id": "task-1", "status": "success"},
    )
    journal.close()
    monkeypatch.setenv("OPENSPACE_EXECUTION_JOURNAL_PATH", str(journal_path))

    app = create_app(
        db_path=tmp_path / "skills.db",
        evidence_db_path=tmp_path / "evidence.db",
        auth_token="",
        observability_roots=[evidence_root],
        auth_audit_path=tmp_path / "audit.db",
    )
    app.testing = True
    response = app.test_client().get("/api/v1/observability")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["evidence"]["verified"] == 1
    assert payload["evidence"]["invalid"] == 0
    assert payload["execution_journal"]["by_state"]["completed"] == 1
    assert payload["quality"]["schema_version"] == "skill_quality_v2"
