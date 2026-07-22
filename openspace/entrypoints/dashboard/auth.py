"""Token identities, role checks, and durable dashboard security audit events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROLES = ("viewer", "operator", "admin")
_ROLE_LEVEL = {role: index for index, role in enumerate(ROLES)}


@dataclass(frozen=True, slots=True)
class DashboardIdentity:
    subject: str
    role: str
    token_fingerprint: str
    auth_method: str = "bearer"

    def has_role(self, minimum_role: str) -> bool:
        return _ROLE_LEVEL[self.role] >= _ROLE_LEVEL[minimum_role]

    def to_public_dict(self) -> dict[str, str]:
        return {
            "subject": self.subject,
            "role": self.role,
            "auth_method": self.auth_method,
            "token_fingerprint": self.token_fingerprint,
        }


class DashboardAuth:
    """In-memory token verifier supporting legacy admin and role-scoped tokens."""

    def __init__(self, credentials: list[tuple[str, DashboardIdentity]]) -> None:
        self._credentials = credentials
        self._cookies = {
            dashboard_auth_cookie_value(token): identity
            for token, identity in credentials
        }

    @property
    def enabled(self) -> bool:
        return bool(self._credentials)

    @classmethod
    def from_config(
        cls,
        *,
        legacy_token: str = "",
        tokens_json: str | None = None,
    ) -> "DashboardAuth":
        credentials: list[tuple[str, DashboardIdentity]] = []
        if legacy_token.strip():
            credentials.append(
                _credential(legacy_token.strip(), subject="legacy-admin", role="admin")
            )
        raw = tokens_json if tokens_json is not None else os.environ.get(
            "OPENSPACE_DASHBOARD_TOKENS_JSON",
            "",
        )
        if raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("OPENSPACE_DASHBOARD_TOKENS_JSON must be valid JSON") from exc
            credentials.extend(_parse_credentials(parsed))
        fingerprints: set[str] = set()
        unique: list[tuple[str, DashboardIdentity]] = []
        for token, identity in credentials:
            if identity.token_fingerprint in fingerprints:
                raise ValueError("duplicate dashboard token configured")
            fingerprints.add(identity.token_fingerprint)
            unique.append((token, identity))
        return cls(unique)

    def authenticate_token(
        self,
        token: str,
        *,
        auth_method: str = "bearer",
    ) -> DashboardIdentity | None:
        if not token:
            return None
        matched: DashboardIdentity | None = None
        for expected, identity in self._credentials:
            if hmac.compare_digest(token, expected):
                matched = DashboardIdentity(
                    identity.subject,
                    identity.role,
                    identity.token_fingerprint,
                    auth_method,
                )
        return matched

    def authenticate_request(
        self,
        authorization: str,
        cookie: str,
    ) -> DashboardIdentity | None:
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() == "bearer" and bearer:
            return self.authenticate_token(bearer, auth_method="bearer")
        if cookie:
            for expected, identity in self._cookies.items():
                if hmac.compare_digest(cookie, expected):
                    return DashboardIdentity(
                        identity.subject,
                        identity.role,
                        identity.token_fingerprint,
                        "cookie",
                    )
        return None

    def cookie_for_token(self, token: str) -> str:
        if self.authenticate_token(token) is None:
            raise ValueError("invalid dashboard token")
        return dashboard_auth_cookie_value(token)


class DashboardAuditLog:
    """Append-only SQLite log that never stores raw tokens or remote addresses."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                subject TEXT NOT NULL,
                role TEXT NOT NULL,
                action TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                outcome TEXT NOT NULL,
                remote_fingerprint TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        self._conn.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def record(
        self,
        *,
        identity: DashboardIdentity | None,
        action: str,
        method: str,
        path: str,
        outcome: str,
        remote_addr: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        remote_fingerprint = (
            hashlib.sha256(remote_addr.encode("utf-8")).hexdigest()[:16]
            if remote_addr
            else ""
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dashboard_audit_events (
                    occurred_at, subject, role, action, method, path, outcome,
                    remote_fingerprint, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    identity.subject if identity else "anonymous",
                    identity.role if identity else "none",
                    str(action),
                    str(method),
                    str(path),
                    str(outcome),
                    remote_fingerprint,
                    json.dumps(
                        dict(details or {}),
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ),
                ),
            )
            self._conn.commit()

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome, COUNT(*) FROM dashboard_audit_events GROUP BY outcome"
            ).fetchall()
        return {"total": sum(int(row[1]) for row in rows), "by_outcome": dict(rows)}


def dashboard_auth_cookie_value(token: str) -> str:
    return hashlib.sha256(f"openspace-dashboard:{token}".encode("utf-8")).hexdigest()


def _credential(token: str, *, subject: str, role: str) -> tuple[str, DashboardIdentity]:
    if not token or len(token) < 8:
        raise ValueError("dashboard tokens must contain at least 8 characters")
    normalized_role = str(role).strip().lower()
    if normalized_role not in _ROLE_LEVEL:
        raise ValueError(f"invalid dashboard role: {role}")
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    return token, DashboardIdentity(str(subject or fingerprint), normalized_role, fingerprint)


def _parse_credentials(value: Any) -> list[tuple[str, DashboardIdentity]]:
    credentials: list[tuple[str, DashboardIdentity]] = []
    if isinstance(value, Mapping):
        items = value.items()
        for token, config in items:
            if isinstance(config, str):
                role, subject = config, f"token-{len(credentials) + 1}"
            elif isinstance(config, Mapping):
                role = str(config.get("role") or "viewer")
                subject = str(config.get("subject") or config.get("name") or f"token-{len(credentials) + 1}")
            else:
                raise ValueError("dashboard token config values must be roles or objects")
            credentials.append(_credential(str(token), subject=subject, role=role))
        return credentials
    if isinstance(value, list):
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                raise ValueError("dashboard token list entries must be objects")
            credentials.append(
                _credential(
                    str(item.get("token") or ""),
                    subject=str(item.get("subject") or item.get("name") or f"token-{index + 1}"),
                    role=str(item.get("role") or "viewer"),
                )
            )
        return credentials
    raise ValueError("OPENSPACE_DASHBOARD_TOKENS_JSON must be an object or list")
