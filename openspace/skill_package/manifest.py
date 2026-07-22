"""Universal Skill Package Spec v1 implementation.

The format is directory-native: ``SKILL.md`` remains the entry point, while a
canonical JSON manifest adds content hashes, compatibility, permissions,
provenance, a lightweight SBOM, and optional HMAC authentication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openspace.skill_engine.skill_utils import parse_frontmatter
from openspace.utils.safe_json_cache import JsonCacheError, atomic_write_json, load_json_object

SKILL_PACKAGE_SCHEMA = "openspace_universal_skill_package_v1"
SKILL_PACKAGE_SCHEMA_VERSION = 1
SKILL_PACKAGE_MANIFEST = "OPENSPACE-SKILL-PACKAGE.json"
_SIGNATURE_ALGORITHM = "hmac-sha256"
_IGNORED_SIDECARS = frozenset(
    {".skill_id", ".cloud_skill.json", ".openspace-upload.json", SKILL_PACKAGE_MANIFEST}
)
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_FILE_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
_MAX_FILES = 4096


class SkillPackageError(ValueError):
    """Raised when a skill package violates integrity or policy constraints."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SkillPackageError(f"package manifest is not canonical JSON: {exc}") from exc


def _identity_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("manifest_id", None)
    payload.pop("signature", None)
    return payload


def _signature_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("signature", None)
    return payload


def _normalize_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = re.split(r"[,\n]", value)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = [value]
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def _files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise SkillPackageError(f"skill packages may not contain symlinks: {path}")
        if path.is_file() and path.name not in _IGNORED_SIDECARS:
            files.append(path)
    if len(files) > _MAX_FILES:
        raise SkillPackageError(f"skill package exceeds {_MAX_FILES} files")
    return files


def _digest_file(path: Path) -> tuple[str, int, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SkillPackageError(f"cannot read package file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SkillPackageError(f"package member is not a regular file: {path}")
        if before.st_size > _MAX_FILE_BYTES:
            raise SkillPackageError(f"package file exceeds size limit: {path}")
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
            raise SkillPackageError(f"package file changed while hashing: {path}")
        return digest.hexdigest(), size, bool(before.st_mode & stat.S_IXUSR)
    finally:
        os.close(descriptor)


def _read_regular_file(path: Path, *, max_bytes: int) -> tuple[bytes, bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SkillPackageError(f"cannot read package file {path}: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SkillPackageError(f"package member is not a regular file: {path}")
        if before.st_size > max_bytes:
            raise SkillPackageError(f"package file exceeds size limit: {path}")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise SkillPackageError(f"package file exceeds size limit: {path}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if size != before.st_size or _file_identity(before) != _file_identity(after):
            raise SkillPackageError(f"package file changed while reading: {path}")
        return b"".join(chunks), bool(before.st_mode & stat.S_IXUSR)
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


def _source_provenance(root: Path) -> dict[str, Any]:
    provenance: dict[str, Any] = {"source_path": root.name}
    try:
        provenance["git_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.strip()
        provenance["git_dirty"] = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", str(root)],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        provenance.update({"git_commit": None, "git_dirty": None})
    return provenance


def _dependency_components(root: Path) -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    requirements = root / "requirements.txt"
    if requirements.is_file() and not requirements.is_symlink():
        try:
            lines = requirements.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        for line in lines:
            value = line.strip()
            if value and not value.startswith(("#", "-")):
                components.append({"type": "python", "requirement": value})
    return components


def build_skill_package_manifest(
    skill_dir: str | Path,
    *,
    version: str = "1.0.0",
    license_expression: str | None = None,
    permissions: Iterable[str] | None = None,
    tools: Iterable[str] | None = None,
    models: Iterable[str] | None = None,
    operating_systems: Iterable[str] | None = None,
    provenance: Mapping[str, Any] | None = None,
    signing_key: str | bytes | None = None,
    signing_key_id: str = "local",
) -> dict[str, Any]:
    """Build and atomically write a package manifest next to ``SKILL.md``."""

    root = Path(skill_dir).expanduser().resolve(strict=True)
    skill_file = root / "SKILL.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        raise SkillPackageError("skill package requires a regular SKILL.md")
    try:
        frontmatter = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise SkillPackageError(f"cannot read SKILL.md: {exc}") from exc
    name = str(frontmatter.get("name") or root.name).strip()
    description = str(frontmatter.get("description") or "").strip()
    if not name or not description:
        raise SkillPackageError("SKILL.md frontmatter requires name and description")
    resolved_license = str(
        license_expression
        or frontmatter.get("license")
        or "NOASSERTION"
    ).strip()

    file_entries: list[dict[str, Any]] = []
    total_bytes = 0
    for path in _files(root):
        digest, size, executable = _digest_file(path)
        total_bytes += size
        if total_bytes > _MAX_TOTAL_BYTES:
            raise SkillPackageError("skill package exceeds total byte limit")
        file_entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
                "size_bytes": size,
                "executable": executable,
            }
        )

    declared_permissions = _normalize_list(
        permissions if permissions is not None else frontmatter.get("permissions")
    )
    declared_tools = _normalize_list(
        tools if tools is not None else (
            frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools")
        )
    )
    declared_models = _normalize_list(
        models if models is not None else frontmatter.get("models")
    )
    declared_os = _normalize_list(
        operating_systems
        if operating_systems is not None
        else frontmatter.get("operating_systems")
    ) or ["any"]
    source = _source_provenance(root)
    source.update(dict(provenance or {}))
    manifest: dict[str, Any] = {
        "schema": SKILL_PACKAGE_SCHEMA,
        "schema_version": SKILL_PACKAGE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "package": {
            "name": name,
            "version": str(version),
            "description": description,
            "entrypoint": "SKILL.md",
            "license": resolved_license,
        },
        "compatibility": {
            "openspace": ">=2.0.0,<3",
            "operating_systems": declared_os,
            "models": declared_models,
        },
        "capabilities": {
            "permissions": declared_permissions,
            "tools": declared_tools,
            "network_domains": _normalize_list(frontmatter.get("network_domains")),
        },
        "provenance": source,
        "sbom": {
            "format": "openspace-sbom-v1",
            "components": _dependency_components(root),
        },
        "files": file_entries,
        "file_summary": {
            "count": len(file_entries),
            "total_bytes": total_bytes,
            "hash_algorithm": "sha256",
        },
    }
    manifest["manifest_id"] = "sha256:" + hashlib.sha256(
        _canonical_bytes(_identity_payload(manifest))
    ).hexdigest()
    if signing_key:
        key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
        manifest["signature"] = {
            "algorithm": _SIGNATURE_ALGORITHM,
            "key_id": str(signing_key_id or "local"),
            "digest": hmac.new(
                key,
                _canonical_bytes(_signature_payload(manifest)),
                hashlib.sha256,
            ).hexdigest(),
        }
    atomic_write_json(root / SKILL_PACKAGE_MANIFEST, manifest)
    return manifest


def verify_skill_package(
    skill_dir: str | Path,
    *,
    signing_key: str | bytes | None = None,
    require_signature: bool = False,
    require_declared_license: bool = False,
) -> dict[str, Any]:
    """Verify manifest identity, policy declarations, and exact file inventory."""

    root = Path(skill_dir).expanduser().resolve(strict=True)
    path = root / SKILL_PACKAGE_MANIFEST
    try:
        manifest = load_json_object(path, max_bytes=_MAX_MANIFEST_BYTES)
    except JsonCacheError as exc:
        raise SkillPackageError(str(exc)) from exc
    errors: list[str] = []
    if manifest.get("schema") != SKILL_PACKAGE_SCHEMA:
        errors.append("unsupported_schema")
    if manifest.get("schema_version") != SKILL_PACKAGE_SCHEMA_VERSION:
        errors.append("unsupported_schema_version")
    expected_id = "sha256:" + hashlib.sha256(
        _canonical_bytes(_identity_payload(manifest))
    ).hexdigest()
    if not hmac.compare_digest(str(manifest.get("manifest_id") or ""), expected_id):
        errors.append("manifest_id_mismatch")

    package = manifest.get("package") if isinstance(manifest.get("package"), Mapping) else {}
    for field_name in ("name", "version", "description", "entrypoint", "license"):
        if not str(package.get(field_name) or "").strip():
            errors.append(f"package_field_missing:{field_name}")
    if package.get("entrypoint") != "SKILL.md":
        errors.append("invalid_entrypoint")
    if require_declared_license and str(package.get("license") or "") == "NOASSERTION":
        errors.append("declared_license_required")

    signature = manifest.get("signature")
    signature_status = "absent"
    if signature is not None:
        if not isinstance(signature, Mapping) or signature.get("algorithm") != _SIGNATURE_ALGORITHM:
            errors.append("signature_invalid")
            signature_status = "invalid"
        elif signing_key is None:
            signature_status = "unverified"
            if require_signature:
                errors.append("signature_key_required")
        else:
            key = signing_key.encode("utf-8") if isinstance(signing_key, str) else signing_key
            expected = hmac.new(
                key,
                _canonical_bytes(_signature_payload(manifest)),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(str(signature.get("digest") or ""), expected):
                signature_status = "verified"
            else:
                errors.append("signature_mismatch")
                signature_status = "invalid"
    elif require_signature:
        errors.append("signature_required")

    declared_entries = manifest.get("files")
    if not isinstance(declared_entries, list):
        declared_entries = []
        errors.append("files_invalid")
    declared_entry_count = len(declared_entries)
    if declared_entry_count > _MAX_FILES:
        errors.append("file_count_exceeded")
        declared_entries = declared_entries[:_MAX_FILES]
    summary = manifest.get("file_summary")
    if not isinstance(summary, Mapping):
        errors.append("file_summary_invalid")
        summary = {}
    if summary.get("count") != declared_entry_count:
        errors.append("file_summary_count_mismatch")
    if summary.get("hash_algorithm") != "sha256":
        errors.append("file_summary_algorithm_invalid")
    declared_total = 0
    declared_total_valid = True
    for item in declared_entries:
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
        errors.append("file_summary_size_invalid")
    elif summary.get("total_bytes") != declared_total:
        errors.append("file_summary_total_mismatch")
    declared: dict[str, Mapping[str, Any]] = {}
    for item in declared_entries:
        if not isinstance(item, Mapping):
            errors.append("file_entry_invalid")
            continue
        relative = str(item.get("path") or "")
        rel_path = Path(relative)
        if (
            not relative
            or rel_path.is_absolute()
            or ".." in rel_path.parts
            or relative in declared
        ):
            errors.append(f"file_path_invalid:{relative}")
            continue
        declared[relative] = item

    actual_paths = _files(root)
    actual = {path.relative_to(root).as_posix(): path for path in actual_paths}
    for relative in sorted(set(declared) - set(actual)):
        errors.append(f"file_missing:{relative}")
    for relative in sorted(set(actual) - set(declared)):
        errors.append(f"file_undeclared:{relative}")
    verified_bytes = 0
    verified_files = 0
    for relative in sorted(set(actual).intersection(declared)):
        entry_errors = len(errors)
        digest, size, executable = _digest_file(actual[relative])
        item = declared[relative]
        if not hmac.compare_digest(str(item.get("sha256") or ""), digest):
            errors.append(f"file_digest_mismatch:{relative}")
        if item.get("size_bytes") != size:
            errors.append(f"file_size_mismatch:{relative}")
        if not isinstance(item.get("executable"), bool) or item.get("executable") != executable:
            errors.append(f"file_mode_mismatch:{relative}")
        if len(errors) == entry_errors:
            verified_files += 1
            verified_bytes += size

    return {
        "ok": not errors,
        "manifest_id": manifest.get("manifest_id"),
        "package": dict(package),
        "signature_status": signature_status,
        "verified_files": verified_files,
        "verified_bytes": verified_bytes,
        "errors": errors,
    }


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def enforce_skill_package_policy(skill_dir: str | Path) -> dict[str, Any]:
    """Enforce configured package policy while retaining legacy compatibility."""

    root = Path(skill_dir).expanduser().resolve(strict=True)
    manifest_path = root / SKILL_PACKAGE_MANIFEST
    require_manifest = _env_bool("OPENSPACE_REQUIRE_SKILL_PACKAGE_MANIFEST")
    require_signature = _env_bool("OPENSPACE_REQUIRE_SIGNED_SKILL_PACKAGES")
    if not manifest_path.exists():
        if require_manifest or require_signature:
            raise SkillPackageError("required skill package manifest is missing")
        return {"status": "legacy", "verified": False}
    signing_key_env = os.environ.get(
        "OPENSPACE_SKILL_PACKAGE_SIGNING_KEY_ENV",
        "OPENSPACE_SKILL_PACKAGE_SIGNING_KEY",
    )
    report = verify_skill_package(
        root,
        signing_key=os.environ.get(signing_key_env),
        require_signature=require_signature,
        require_declared_license=_env_bool("OPENSPACE_REQUIRE_SKILL_PACKAGE_LICENSE"),
    )
    if not report["ok"]:
        raise SkillPackageError(
            "skill package verification failed: " + ", ".join(report["errors"])
        )
    return {"status": "verified", "verified": True, **report}


def build_skill_archive(
    skill_dir: str | Path,
    output_path: str | Path,
    *,
    signing_key: str | bytes | None = None,
    require_signature: bool = False,
    overwrite: bool = False,
) -> Path:
    """Create a deterministic ZIP after verifying the package manifest."""

    root = Path(skill_dir).expanduser().resolve(strict=True)
    raw_output = Path(output_path).expanduser()
    if raw_output.is_symlink():
        raise SkillPackageError("archive output may not be a symlink")
    output = raw_output.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise SkillPackageError("archive output must be outside the skill package")
    if output.exists() and not overwrite:
        raise SkillPackageError(f"archive already exists: {output}")
    report = verify_skill_package(
        root,
        signing_key=signing_key,
        require_signature=require_signature,
    )
    if not report["ok"]:
        raise SkillPackageError("cannot archive invalid skill package")
    manifest_path = root / SKILL_PACKAGE_MANIFEST
    manifest_bytes, manifest_executable = _read_regular_file(
        manifest_path,
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        current_manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SkillPackageError(f"package manifest changed during archive: {exc}") from exc
    if not isinstance(current_manifest, Mapping):
        raise SkillPackageError("package manifest changed during archive")
    if current_manifest.get("manifest_id") != report.get("manifest_id"):
        raise SkillPackageError("package manifest changed during archive")
    declared = {
        str(item.get("path")): item
        for item in current_manifest.get("files", [])
        if isinstance(item, Mapping)
    }
    captured: list[tuple[str, bytes, bool]] = []
    actual_paths = _files(root)
    if {path.relative_to(root).as_posix() for path in actual_paths} != set(declared):
        raise SkillPackageError("package inventory changed during archive")
    for path in actual_paths:
        relative = path.relative_to(root).as_posix()
        payload, executable = _read_regular_file(path, max_bytes=_MAX_FILE_BYTES)
        item = declared[relative]
        if (
            not hmac.compare_digest(
                str(item.get("sha256") or ""),
                hashlib.sha256(payload).hexdigest(),
            )
            or item.get("size_bytes") != len(payload)
            or item.get("executable") != executable
        ):
            raise SkillPackageError(f"package member changed during archive: {relative}")
        captured.append((relative, payload, executable))
    captured.append((SKILL_PACKAGE_MANIFEST, manifest_bytes, manifest_executable))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, payload, executable in sorted(captured):
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            mode = 0o755 if executable else 0o644
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return output
