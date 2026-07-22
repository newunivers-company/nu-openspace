"""Signed approval and budget boundary for NU resource execution."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from openspace.utils.safe_json_cache import JsonCacheError, atomic_write_json, load_json_object

NU_APPROVAL_SCHEMA = "openspace_nu_resource_approval_v1"
NU_APPROVAL_VERSION = 1
_SIGNATURE_ALGORITHM = "hmac-sha256"
_MAX_APPROVAL_BYTES = 1024 * 1024
_MAX_LEDGER_RESULT_BYTES = 1024 * 1024
_MAX_CLOCK_SKEW_SECONDS = 300


class NuGovernanceError(RuntimeError):
    """Raised when a governed NU operation is not explicitly authorized."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise NuGovernanceError(f"approval data must be canonical JSON: {exc}") from exc


def _approval_identity_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("approval_id", None)
    payload.pop("signature", None)
    return payload


def _approval_signature_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(value)
    payload.pop("signature", None)
    return payload


def _request_digest(prompt: str, params: Mapping[str, Any], media: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical_bytes(
            {"prompt": str(prompt), "params": dict(params), "media": dict(media)}
        )
    ).hexdigest()


def create_resource_approval(
    output_path: str | Path,
    *,
    candidate_ids: Sequence[str],
    prompt: str,
    params: Mapping[str, Any] | None = None,
    media: Mapping[str, Any] | None = None,
    max_cost: float = 0.0,
    cost_unit: str = "local",
    allow_remote: bool = False,
    max_uses: int = 1,
    expires_in_seconds: int = 3600,
    subject: str = "operator",
    signing_key: str | bytes,
    signing_key_id: str = "local",
) -> dict[str, Any]:
    """Create a request-bound, time-limited resource approval."""

    if not isinstance(params or {}, Mapping) or not isinstance(media or {}, Mapping):
        raise NuGovernanceError("approval params and media must be mappings")
    raw_candidates = [candidate_ids] if isinstance(candidate_ids, str) else candidate_ids
    candidates = list(
        dict.fromkeys(
            str(item).strip() for item in raw_candidates if str(item).strip()
        )
    )
    if not candidates:
        raise NuGovernanceError("at least one candidate_id is required")
    if not signing_key:
        raise NuGovernanceError("approval signing key is required")
    maximum_cost = _nonnegative_finite_cost(max_cost, "max_cost")
    if isinstance(max_uses, bool) or int(max_uses) < 1:
        raise NuGovernanceError("max_uses must be at least one")
    if isinstance(expires_in_seconds, bool) or int(expires_in_seconds) < 1:
        raise NuGovernanceError("expires_in_seconds must be positive")
    normalized_cost_unit = str(cost_unit).strip().lower()
    if not normalized_cost_unit:
        raise NuGovernanceError("cost_unit is required")
    normalized_subject = str(subject).strip()
    if not normalized_subject:
        raise NuGovernanceError("approval subject is required")
    now = datetime.now(timezone.utc)
    request_digest = _request_digest(prompt, params or {}, media or {})
    approval: dict[str, Any] = {
        "schema": NU_APPROVAL_SCHEMA,
        "schema_version": NU_APPROVAL_VERSION,
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=int(expires_in_seconds))).isoformat(),
        "subject": normalized_subject,
        "nonce": uuid.uuid4().hex,
        "scope": {
            "operations": ["generate"],
            "candidate_ids": candidates,
            "allow_remote": bool(allow_remote),
            "max_cost": maximum_cost,
            "cost_unit": normalized_cost_unit,
            "max_uses": int(max_uses),
            "request_sha256": request_digest,
        },
    }
    approval["approval_id"] = "sha256:" + hashlib.sha256(
        _canonical_bytes(_approval_identity_payload(approval))
    ).hexdigest()
    key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
    approval["signature"] = {
        "algorithm": _SIGNATURE_ALGORITHM,
        "key_id": str(signing_key_id or "local"),
        "digest": hmac.new(
            key,
            _canonical_bytes(_approval_signature_payload(approval)),
            hashlib.sha256,
        ).hexdigest(),
    }
    atomic_write_json(Path(output_path), approval)
    return approval


def verify_resource_approval(
    approval_path: str | Path,
    *,
    signing_key: str | bytes,
    candidate_id: str,
    prompt: str,
    params: Mapping[str, Any],
    media: Mapping[str, Any],
    operation: str = "generate",
    is_remote: bool,
    estimated_cost: float | None,
    cost_unit: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify signature, expiry, scope, request binding, and estimated budget."""

    if not signing_key:
        raise NuGovernanceError("approval signing key is required")
    path = Path(approval_path).expanduser()
    if path.is_symlink():
        raise NuGovernanceError("approval file may not be a symlink")
    try:
        approval = load_json_object(path, max_bytes=_MAX_APPROVAL_BYTES)
    except JsonCacheError as exc:
        raise NuGovernanceError(str(exc)) from exc
    errors: list[str] = []
    if approval.get("schema") != NU_APPROVAL_SCHEMA or approval.get("schema_version") != 1:
        errors.append("unsupported_approval_schema")
    expected_id = "sha256:" + hashlib.sha256(
        _canonical_bytes(_approval_identity_payload(approval))
    ).hexdigest()
    if not hmac.compare_digest(str(approval.get("approval_id") or ""), expected_id):
        errors.append("approval_id_mismatch")
    signature = approval.get("signature")
    if not isinstance(signature, Mapping) or signature.get("algorithm") != _SIGNATURE_ALGORITHM:
        errors.append("approval_signature_invalid")
    else:
        key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        expected_signature = hmac.new(
            key,
            _canonical_bytes(_approval_signature_payload(approval)),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(str(signature.get("digest") or ""), expected_signature):
            errors.append("approval_signature_mismatch")
    scope = approval.get("scope") if isinstance(approval.get("scope"), Mapping) else {}
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    issued: datetime | None = None
    expires: datetime | None = None
    try:
        issued = datetime.fromisoformat(str(approval.get("issued_at") or ""))
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        if issued.astimezone(timezone.utc) > reference.astimezone(timezone.utc) + timedelta(
            seconds=_MAX_CLOCK_SKEW_SECONDS
        ):
            errors.append("approval_issued_in_future")
    except ValueError:
        errors.append("approval_issued_at_invalid")
    try:
        expires = datetime.fromisoformat(str(approval.get("expires_at") or ""))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if reference.astimezone(timezone.utc) >= expires.astimezone(timezone.utc):
            errors.append("approval_expired")
    except ValueError:
        errors.append("approval_expiry_invalid")
    if issued is not None and expires is not None and expires <= issued:
        errors.append("approval_time_window_invalid")
    operations = scope.get("operations")
    candidate_ids = scope.get("candidate_ids")
    if not isinstance(operations, list) or any(not isinstance(item, str) for item in operations):
        operations = []
        errors.append("approval_operations_invalid")
    if not isinstance(candidate_ids, list) or any(
        not isinstance(item, str) for item in candidate_ids
    ):
        candidate_ids = []
        errors.append("approval_candidates_invalid")
    if operation not in operations:
        errors.append("operation_not_approved")
    if candidate_id not in candidate_ids:
        errors.append("candidate_not_approved")
    if is_remote and not bool(scope.get("allow_remote")):
        errors.append("remote_execution_not_approved")
    if not hmac.compare_digest(
        str(scope.get("request_sha256") or ""),
        _request_digest(prompt, params, media),
    ):
        errors.append("request_digest_mismatch")
    try:
        maximum = _nonnegative_finite_cost(scope.get("max_cost"), "approval max_cost")
    except NuGovernanceError:
        maximum = -1.0
        errors.append("approval_budget_invalid")
    approved_cost_unit = str(scope.get("cost_unit") or "").strip().lower()
    requested_cost_unit = str(cost_unit).strip().lower()
    if not approved_cost_unit or approved_cost_unit != requested_cost_unit:
        errors.append("approval_cost_unit_mismatch")
    try:
        estimate = _nonnegative_finite_cost(estimated_cost, "estimated_cost")
    except NuGovernanceError:
        estimate = None
        errors.append("estimated_cost_invalid")
    if estimate is not None and estimate > maximum + 1e-12:
        errors.append("estimated_cost_exceeds_approval")
    try:
        max_uses = int(scope.get("max_uses"))
    except (TypeError, ValueError):
        max_uses = 0
        errors.append("approval_max_uses_invalid")
    if isinstance(scope.get("max_uses"), bool) or max_uses < 1:
        errors.append("approval_max_uses_invalid")
    if errors:
        raise NuGovernanceError("resource approval rejected: " + ", ".join(dict.fromkeys(errors)))
    return {
        "approval_id": approval["approval_id"],
        "subject": str(approval.get("subject") or "operator"),
        "max_cost": maximum,
        "cost_unit": approved_cost_unit,
        "max_uses": max_uses,
        "allow_remote": bool(scope.get("allow_remote")),
        "expires_at": str(approval.get("expires_at")),
        "signature_key_id": str(signature.get("key_id") or ""),
    }


class NuExecutionLedger:
    """Atomic approval-use reservation ledger preventing replay across processes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nu_resource_approval_uses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                reserved_at TEXT NOT NULL,
                completed_at TEXT,
                result_json TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_nu_approval_uses ON nu_resource_approval_uses(approval_id, id)"
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def reserve(self, *, approval_id: str, candidate_id: str, request_sha256: str, max_uses: int) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                used = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM nu_resource_approval_uses WHERE approval_id=?",
                        (approval_id,),
                    ).fetchone()[0]
                )
                if used >= max_uses:
                    raise NuGovernanceError("resource approval use limit exhausted")
                cursor = self._conn.execute(
                    """
                    INSERT INTO nu_resource_approval_uses (
                        approval_id, candidate_id, request_sha256, status, reserved_at
                    ) VALUES (?, ?, ?, 'reserved', ?)
                    """,
                    (
                        approval_id,
                        candidate_id,
                        request_sha256,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                self._conn.commit()
                return int(cursor.lastrowid)
            except Exception:
                self._conn.rollback()
                raise

    def complete(self, row_id: int, *, status: str, result: Mapping[str, Any]) -> None:
        result_json = json.dumps(
            dict(result),
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        encoded = result_json.encode("utf-8")
        if len(encoded) > _MAX_LEDGER_RESULT_BYTES:
            result_json = json.dumps(
                {
                    "status": status,
                    "error": "ledger_result_exceeded_limit",
                    "result_sha256": hashlib.sha256(encoded).hexdigest(),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        with self._lock:
            self._conn.execute(
                """
                UPDATE nu_resource_approval_uses SET status=?, completed_at=?, result_json=?
                WHERE id=?
                """,
                (
                    status,
                    datetime.now(timezone.utc).isoformat(),
                    result_json,
                    row_id,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def governed_resource_execute(
    *,
    candidate_id: str,
    prompt: str = "",
    params_json: str = "{}",
    media_json: str = "{}",
    approval_path: str = "",
    dry_run: bool = True,
    generator: Any | None = None,
    ledger: NuExecutionLedger | None = None,
) -> dict[str, Any]:
    """Plan by default; live execution requires environment and signed approval."""

    from nu_resource_gen_lib.api import ResourceGenerator, ResourceRequest

    params = _json_object(params_json, "params")
    media = _json_object(media_json, "media")
    resource_generator = generator or ResourceGenerator(
        record_ledger=True,
        archive_artifacts=True,
        require_artifact_archive=False,
    )
    spec = resource_generator.get_candidate(candidate_id.strip())
    request = ResourceRequest(prompt=prompt, params=params, media=media)
    policy = resource_generator.execution_policy_status(spec.candidate_id, request)
    quality_gates = (
        spec.extras.get("quality_presets", {})
        .get("presets", {})
        .get(request.quality_preset or "balanced", {})
        .get("acceptance_gates", [])
    )
    plan = {
        "candidate": _candidate_public(spec),
        "policy": policy,
        "quality_gates": list(quality_gates),
        "dry_run": bool(dry_run),
        "executed": False,
        "approval_required": True,
    }
    if dry_run:
        recorder = getattr(resource_generator, "record_dry_run", None)
        ledger_status = recorder(spec.candidate_id, request) if callable(recorder) else None
        return {**plan, "status": "planned", "ledger": ledger_status}

    if os.environ.get("OPENSPACE_NU_RESOURCE_EXECUTION_ENABLED", "").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        raise NuGovernanceError("live NU resource execution is disabled")
    capability = os.environ.get("OPENSPACE_NU_RESOURCE_CAPABILITY_TOKEN", "").strip()
    if len(capability) < 16:
        raise NuGovernanceError("live NU resource capability token is not configured")
    if bool(policy.get("blocked")):
        raise NuGovernanceError(
            "nu-resource-gen execution policy blocked the request: "
            + ", ".join(str(item) for item in policy.get("blockers", []))
        )
    if not approval_path:
        raise NuGovernanceError("signed approval_path is required for live execution")
    signing_key_env = os.environ.get(
        "OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY_ENV",
        "OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY",
    )
    signing_key = os.environ.get(signing_key_env, "")
    if not signing_key:
        raise NuGovernanceError(f"approval signing key is missing: {signing_key_env}")
    is_remote = not _candidate_is_local(spec)
    approval = verify_resource_approval(
        approval_path,
        signing_key=signing_key,
        candidate_id=spec.candidate_id,
        prompt=prompt,
        params=params,
        media=media,
        is_remote=is_remote,
        estimated_cost=spec.cost,
        cost_unit=spec.cost_unit,
    )
    owns_ledger = ledger is None
    execution_ledger = ledger or NuExecutionLedger(
        os.environ.get(
            "OPENSPACE_NU_RESOURCE_EXECUTION_LEDGER_PATH",
            ".openspace/nu-resource-execution.db",
        )
    )
    try:
        request_sha256 = _request_digest(prompt, params, media)
        reservation = execution_ledger.reserve(
            approval_id=approval["approval_id"],
            candidate_id=spec.candidate_id,
            request_sha256=request_sha256,
            max_uses=approval["max_uses"],
        )
        try:
            result = resource_generator.generate(spec.candidate_id, request)
        except Exception as exc:
            execution_ledger.complete(
                reservation,
                status="failed",
                result={"error": type(exc).__name__, "candidate_id": spec.candidate_id},
            )
            raise
        public_result = _result_public(result)
        budget_reasons = _actual_cost_budget_reasons(public_result, approval)
        budget_violation = bool(budget_reasons)
        status = "completed_budget_violation" if budget_violation else "completed"
        response = {
            **plan,
            "status": status,
            "dry_run": False,
            "executed": True,
            "approval": approval,
            "result": public_result,
            "budget_violation": budget_violation,
            "budget_violation_reasons": budget_reasons,
        }
        execution_ledger.complete(reservation, status=status, result=response)
        return response
    finally:
        if owns_ledger:
            execution_ledger.close()


def governed_resource_execution_tool_enabled() -> bool:
    return os.environ.get("OPENSPACE_NU_RESOURCE_EXECUTION_TOOL_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _json_object(value: str, field: str) -> dict[str, Any]:
    if len(value) > 64 * 1024:
        raise NuGovernanceError(f"{field} JSON exceeds 64 KiB")
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise NuGovernanceError(f"{field} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise NuGovernanceError(f"{field} must be a JSON object")
    return parsed


def _candidate_is_local(spec: Any) -> bool:
    execution = str(getattr(spec, "extras", {}).get("execution") or "").lower()
    provider = str(getattr(spec, "provider", "") or "").lower()
    return (
        execution.startswith(("local", "comfyui"))
        or provider in {"comfyui", "ollama", "local", "local_audio", "local_vlm"}
    )


def _nonnegative_finite_cost(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise NuGovernanceError(f"{field} must be a finite non-negative number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise NuGovernanceError(
            f"{field} must be a finite non-negative number"
        ) from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise NuGovernanceError(f"{field} must be a finite non-negative number")
    return parsed


def _actual_cost_budget_reasons(
    result: Mapping[str, Any],
    approval: Mapping[str, Any],
) -> list[str]:
    reasons: list[str] = []
    try:
        actual_cost = _nonnegative_finite_cost(result.get("cost"), "actual_cost")
    except NuGovernanceError:
        actual_cost = None
        reasons.append("actual_cost_missing_or_invalid")
    actual_unit = str(result.get("cost_unit") or "").strip().lower()
    approved_unit = str(approval.get("cost_unit") or "").strip().lower()
    if not actual_unit or actual_unit != approved_unit:
        reasons.append("actual_cost_unit_mismatch")
    if actual_cost is not None and actual_cost > float(approval["max_cost"]) + 1e-12:
        reasons.append("actual_cost_exceeds_approval")
    return reasons


def _candidate_public(spec: Any) -> dict[str, Any]:
    return {
        "candidate_id": spec.candidate_id,
        "provider": spec.provider,
        "category": spec.category,
        "model": spec.model,
        "cost": spec.cost,
        "cost_unit": spec.cost_unit,
        "local": _candidate_is_local(spec),
    }


def _result_public(result: Any) -> dict[str, Any]:
    fields = (
        "candidate_id", "provider", "model", "output_text", "asset_uri", "job_id",
        "status", "latency_ms", "cost", "cost_unit", "usage", "request_id",
        "routing_decision_id", "attempt_id", "artifact_id", "pipeline_run_id",
        "policy_version",
    )
    if is_dataclass(result):
        raw = asdict(result)
    elif isinstance(result, Mapping):
        raw = dict(result)
    else:
        raw = {name: getattr(result, name, None) for name in fields}
    public: dict[str, Any] = {}
    for name in fields:
        value = raw.get(name)
        if value is None or value == "" or value == {}:
            continue
        public[name] = value
    return public
