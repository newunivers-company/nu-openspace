from pathlib import Path

import pytest

from openspace.entrypoints.dashboard.server import (
    create_app,
    is_loopback_host,
    validate_dashboard_bind,
)
from openspace.entrypoints.mcp.server import validate_mcp_http_bind


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_dashboard_loopback_hosts_are_accepted(host: str) -> None:
    assert is_loopback_host(host)
    validate_dashboard_bind(host, "")


def test_dashboard_remote_bind_requires_token() -> None:
    with pytest.raises(ValueError, match="non-loopback"):
        validate_dashboard_bind("0.0.0.0", "")

    validate_dashboard_bind("0.0.0.0", "long-random-token")


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
