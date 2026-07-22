"""Content-addressed run manifests with optional HMAC authentication.

The manifest deliberately contains only JSON data and file digests.  It never
deserializes executable objects and never follows symlinks while hashing run
artifacts.  Replay is a separate, double-opt-in operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openspace.utils.safe_json_cache import JsonCacheError, atomic_write_json, load_json_object

EVIDENCE_SCHEMA = "openspace_evidence_manifest_v3"
EVIDENCE_SCHEMA_VERSION = 3
EVIDENCE_MANIFEST_FILENAME = "evidence-manifest-v3.json"
_SIGNATURE_ALGORITHM = "hmac-sha256"
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_DEFAULT_MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_DEFAULT_MAX_ARTIFACTS = 4096


class EvidenceManifestError(ValueError):
    """Raised when evidence cannot be safely created, verified, or replayed."""


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
        raise EvidenceManifestError(f"manifest is not canonical JSON: {exc}") from exc


def _manifest_identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("manifest_id", None)
    payload.pop("signature", None)
    return payload


def _signature_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("signature", None)
    return payload


def _sha256_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EvidenceManifestError(f"cannot open artifact {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceManifestError(f"artifact is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise EvidenceManifestError(
                f"artifact exceeds size limit ({before.st_size} > {max_bytes}): {path}"
            )
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if size != before.st_size or _file_identity(before) != _file_identity(after):
            raise EvidenceManifestError(f"artifact changed while hashing: {path}")
        return digest.hexdigest(), size
    finally:
        os.close(descriptor)


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _safe_artifact_path(root: Path, value: str | Path) -> tuple[Path, str]:
    raw = Path(value)
    try:
        relative_input = raw.relative_to(root) if raw.is_absolute() else raw
    except ValueError as exc:
        raise EvidenceManifestError(f"artifact escapes evidence root: {value}") from exc
    if (
        not relative_input.parts
        or relative_input.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_input.parts)
    ):
        raise EvidenceManifestError(f"invalid artifact path: {value}")
    candidate = root
    for part in relative_input.parts:
        candidate /= part
        if candidate.is_symlink():
            raise EvidenceManifestError(f"refusing symlinked artifact path: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise EvidenceManifestError(f"artifact escapes evidence root: {value}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise EvidenceManifestError(f"invalid artifact path: {value}")
    return resolved, relative.as_posix()


def _safe_output_path(root: Path, value: str | Path) -> Path:
    raw = Path(value).expanduser()
    try:
        relative = raw.relative_to(root) if raw.is_absolute() else raw
    except ValueError as exc:
        raise EvidenceManifestError("manifest output must be inside the evidence root") from exc
    if (
        not relative.parts
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise EvidenceManifestError("manifest output path is invalid")
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise EvidenceManifestError("manifest output path may not contain symlinks")
    try:
        candidate.parent.resolve().relative_to(root)
    except ValueError as exc:
        raise EvidenceManifestError("manifest output must be inside the evidence root") from exc
    if candidate.exists() and not candidate.is_file():
        raise EvidenceManifestError("manifest output must be a regular file")
    return candidate


def discover_artifacts(
    root: str | Path,
    *,
    exclude: Iterable[str] = (EVIDENCE_MANIFEST_FILENAME,),
) -> list[Path]:
    """Return regular, non-symlink files below ``root`` in stable order."""

    resolved_root = Path(root).expanduser().resolve(strict=True)
    excluded = {str(item) for item in exclude}
    paths: list[Path] = []
    for candidate in sorted(resolved_root.rglob("*")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        relative = candidate.relative_to(resolved_root).as_posix()
        if relative in excluded:
            continue
        paths.append(candidate)
    return paths


def _source_snapshot(workspace_root: Path | None) -> dict[str, Any]:
    snapshot: dict[str, Any] = {"workspace": str(workspace_root) if workspace_root else None}
    if workspace_root is None:
        return snapshot
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=workspace_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        )
        snapshot.update({"git_commit": commit, "git_dirty": dirty})
    except (OSError, subprocess.SubprocessError):
        snapshot.update({"git_commit": None, "git_dirty": None})
    return snapshot


def create_run_manifest(
    root: str | Path,
    *,
    run: Mapping[str, Any],
    artifact_paths: Iterable[str | Path] | None = None,
    inventory: Mapping[str, Any] | None = None,
    workspace_root: str | Path | None = None,
    replay_argv: Sequence[str] | None = None,
    replay_cwd: str = ".",
    signing_key: str | bytes | None = None,
    signing_key_id: str = "local",
    output_path: str | Path | None = None,
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    max_artifacts: int = _DEFAULT_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Create and atomically persist an Evidence Plane v3 run manifest."""

    resolved_root = Path(root).expanduser().resolve(strict=True)
    output = _safe_output_path(
        resolved_root,
        output_path if output_path is not None else EVIDENCE_MANIFEST_FILENAME,
    )
    output_relative = output.relative_to(resolved_root).as_posix()

    candidates = (
        list(artifact_paths)
        if artifact_paths is not None
        else discover_artifacts(resolved_root)
    )
    if len(candidates) > max_artifacts:
        raise EvidenceManifestError(
            f"artifact count exceeds limit ({len(candidates)} > {max_artifacts})"
        )

    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_bytes = 0
    for value in candidates:
        path, relative = _safe_artifact_path(resolved_root, value)
        if relative == output_relative or relative in seen:
            continue
        digest, size = _sha256_file(path, max_bytes=max_artifact_bytes)
        total_bytes += size
        if total_bytes > max_total_bytes:
            raise EvidenceManifestError(
                f"artifact bytes exceed total limit ({total_bytes} > {max_total_bytes})"
            )
        artifacts.append(
            {
                "path": relative,
                "sha256": digest,
                "size_bytes": size,
            }
        )
        seen.add(relative)

    workspace = Path(workspace_root).expanduser().resolve() if workspace_root else None
    replay: dict[str, Any] = {"available": False}
    if replay_argv:
        argv = [str(item) for item in replay_argv]
        if not argv or any(not item for item in argv):
            raise EvidenceManifestError("replay argv must contain non-empty strings")
        cwd_path = Path(replay_cwd)
        if cwd_path.is_absolute() or ".." in cwd_path.parts:
            raise EvidenceManifestError("replay cwd must stay relative to the evidence root")
        replay = {"available": True, "argv": argv, "cwd": cwd_path.as_posix()}

    manifest: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run": dict(run),
        "source": _source_snapshot(workspace),
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "inventory": dict(inventory or {}),
        "artifacts": artifacts,
        "artifact_summary": {
            "count": len(artifacts),
            "total_bytes": total_bytes,
            "hash_algorithm": "sha256",
        },
        "replay": replay,
    }
    manifest["manifest_id"] = "sha256:" + hashlib.sha256(
        _canonical_bytes(_manifest_identity_payload(manifest))
    ).hexdigest()

    if signing_key:
        key_bytes = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        manifest["signature"] = {
            "algorithm": _SIGNATURE_ALGORITHM,
            "key_id": str(signing_key_id or "local"),
            "digest": hmac.new(
                key_bytes,
                _canonical_bytes(_signature_payload(manifest)),
                hashlib.sha256,
            ).hexdigest(),
        }

    atomic_write_json(output, manifest)
    return manifest


def load_manifest(path: str | Path) -> dict[str, Any]:
    try:
        return load_json_object(Path(path), max_bytes=_MAX_MANIFEST_BYTES)
    except JsonCacheError as exc:
        raise EvidenceManifestError(str(exc)) from exc


def verify_manifest(
    path: str | Path,
    *,
    signing_key: str | bytes | None = None,
    require_signature: bool = False,
    max_artifact_bytes: int = _DEFAULT_MAX_ARTIFACT_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
    max_artifacts: int = _DEFAULT_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Verify structure, identity, signature policy, and every artifact digest."""

    manifest_path = Path(path).expanduser().resolve(strict=True)
    manifest = load_manifest(manifest_path)
    errors: list[str] = []
    if manifest.get("schema") != EVIDENCE_SCHEMA:
        errors.append("unsupported_schema")
    if manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        errors.append("unsupported_schema_version")

    expected_id = "sha256:" + hashlib.sha256(
        _canonical_bytes(_manifest_identity_payload(manifest))
    ).hexdigest()
    if not hmac.compare_digest(str(manifest.get("manifest_id") or ""), expected_id):
        errors.append("manifest_id_mismatch")

    signature = manifest.get("signature")
    signature_status = "absent"
    if signature is not None:
        if not isinstance(signature, Mapping) or signature.get("algorithm") != _SIGNATURE_ALGORITHM:
            errors.append("signature_invalid")
            signature_status = "invalid"
        elif signing_key is None:
            if require_signature:
                errors.append("signature_key_required")
            signature_status = "unverified"
        else:
            key_bytes = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
            expected = hmac.new(
                key_bytes,
                _canonical_bytes(_signature_payload(manifest)),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(str(signature.get("digest") or ""), expected):
                signature_status = "verified"
            else:
                signature_status = "invalid"
                errors.append("signature_mismatch")
    elif require_signature:
        errors.append("signature_required")

    root = manifest_path.parent.resolve()
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        errors.append("artifacts_invalid")
        artifacts = []
    declared_artifact_count = len(artifacts)
    if declared_artifact_count > max_artifacts:
        errors.append("artifact_count_exceeded")
        artifacts = artifacts[:max_artifacts]
    summary = manifest.get("artifact_summary")
    if not isinstance(summary, Mapping):
        errors.append("artifact_summary_invalid")
        summary = {}
    if summary.get("count") != declared_artifact_count:
        errors.append("artifact_summary_count_mismatch")
    if summary.get("hash_algorithm") != "sha256":
        errors.append("artifact_summary_algorithm_invalid")
    declared_total = 0
    declared_total_valid = True
    for item in artifacts:
        size_value = item.get("size_bytes") if isinstance(item, Mapping) else None
        if (
            not isinstance(size_value, int)
            or isinstance(size_value, bool)
            or size_value < 0
        ):
            declared_total_valid = False
            continue
        declared_total += size_value
    if not declared_total_valid:
        errors.append("artifact_summary_size_invalid")
    elif summary.get("total_bytes") != declared_total:
        errors.append("artifact_summary_total_mismatch")
    seen: set[str] = set()
    verified_count = 0
    verified_bytes = 0
    inspected_bytes = 0
    for item in artifacts:
        entry_errors = len(errors)
        if not isinstance(item, Mapping):
            errors.append("artifact_entry_invalid")
            continue
        relative = str(item.get("path") or "")
        if relative in seen:
            errors.append(f"artifact_duplicate:{relative}")
            continue
        seen.add(relative)
        try:
            artifact, safe_relative = _safe_artifact_path(root, relative)
            digest, size = _sha256_file(artifact, max_bytes=max_artifact_bytes)
        except EvidenceManifestError:
            errors.append(f"artifact_unreadable:{relative}")
            continue
        if safe_relative != relative:
            errors.append(f"artifact_path_noncanonical:{relative}")
        if not hmac.compare_digest(str(item.get("sha256") or ""), digest):
            errors.append(f"artifact_digest_mismatch:{relative}")
        if item.get("size_bytes") != size:
            errors.append(f"artifact_size_mismatch:{relative}")
        inspected_bytes += size
        if inspected_bytes > max_total_bytes:
            errors.append("artifact_total_bytes_exceeded")
            break
        if len(errors) == entry_errors:
            verified_count += 1
            verified_bytes += size

    return {
        "ok": not errors,
        "manifest_id": manifest.get("manifest_id"),
        "signature_status": signature_status,
        "verified_artifacts": verified_count,
        "verified_bytes": verified_bytes,
        "errors": errors,
    }


def replay_manifest(
    path: str | Path,
    *,
    execute: bool = False,
    signing_key: str | bytes | None = None,
    require_signature: bool = False,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Verify and plan or execute a recorded replay command without a shell."""

    # A content hash detects accidental changes, but only an authenticated
    # signature prevents an attacker from replacing both payload and hash.
    # Therefore every executable replay requires a verifiable signature.
    effective_require_signature = require_signature or execute
    verification = verify_manifest(
        path,
        signing_key=signing_key,
        require_signature=effective_require_signature,
    )
    if not verification["ok"]:
        raise EvidenceManifestError("manifest verification failed before replay")
    manifest_path = Path(path).expanduser().resolve(strict=True)
    manifest = load_manifest(manifest_path)
    replay = manifest.get("replay")
    if not isinstance(replay, Mapping) or not replay.get("available"):
        raise EvidenceManifestError("manifest does not contain a replay command")
    argv = replay.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v for v in argv):
        raise EvidenceManifestError("manifest replay argv is invalid")
    cwd_value = str(replay.get("cwd") or ".")
    cwd_path = Path(cwd_value)
    if cwd_path.is_absolute() or ".." in cwd_path.parts:
        raise EvidenceManifestError("manifest replay cwd escapes evidence root")
    cwd = (manifest_path.parent / cwd_path).resolve()
    try:
        cwd.relative_to(manifest_path.parent.resolve())
    except ValueError as exc:
        raise EvidenceManifestError("manifest replay cwd escapes evidence root") from exc
    plan = {
        "status": "dry_run",
        "manifest_id": manifest.get("manifest_id"),
        "argv": list(argv),
        "cwd": str(cwd),
        "verification": verification,
    }
    if not execute:
        return plan
    if os.environ.get("OPENSPACE_EVIDENCE_REPLAY_ENABLED", "").strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        raise EvidenceManifestError(
            "live replay requires OPENSPACE_EVIDENCE_REPLAY_ENABLED=1"
        )
    completed = subprocess.run(
        argv,
        cwd=cwd,
        shell=False,
        capture_output=True,
        text=True,
        timeout=max(0.1, float(timeout_s)),
        check=False,
    )
    return {
        **plan,
        "status": "completed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
