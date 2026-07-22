from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from openspace.evidence import (
    EVIDENCE_MANIFEST_FILENAME,
    EvidenceManifestError,
    create_run_manifest,
    replay_manifest,
    verify_manifest,
)


def test_signed_manifest_detects_artifact_tampering(tmp_path: Path) -> None:
    artifact = tmp_path / "transcript.jsonl"
    artifact.write_text('{"role":"assistant"}\n', encoding="utf-8")
    manifest = create_run_manifest(
        tmp_path,
        run={"task_id": "task-1", "status": "completed"},
        signing_key="test-signing-key",
        signing_key_id="test",
    )

    assert manifest["schema_version"] == 3
    report = verify_manifest(
        tmp_path / EVIDENCE_MANIFEST_FILENAME,
        signing_key="test-signing-key",
        require_signature=True,
    )
    assert report["ok"] is True
    assert report["signature_status"] == "verified"

    artifact.write_text("tampered", encoding="utf-8")
    report = verify_manifest(
        tmp_path / EVIDENCE_MANIFEST_FILENAME,
        signing_key="test-signing-key",
        require_signature=True,
    )
    assert report["ok"] is False
    assert "artifact_digest_mismatch:transcript.jsonl" in report["errors"]


def test_manifest_refuses_artifact_outside_root_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "run"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(EvidenceManifestError, match="escapes"):
        create_run_manifest(root, run={"task_id": "task-1"}, artifact_paths=[outside])

    link = root / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(EvidenceManifestError, match="symlink"):
        create_run_manifest(root, run={"task_id": "task-1"}, artifact_paths=[link])

    real = root / "real"
    real.mkdir()
    (real / "nested.txt").write_text("data", encoding="utf-8")
    (root / "linked-dir").symlink_to(real, target_is_directory=True)
    with pytest.raises(EvidenceManifestError, match="symlink"):
        create_run_manifest(
            root,
            run={"task_id": "task-1"},
            artifact_paths=[root / "linked-dir" / "nested.txt"],
        )


def test_replay_is_dry_run_and_double_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "input.txt").write_text("evidence", encoding="utf-8")
    create_run_manifest(
        tmp_path,
        run={"task_id": "task-1"},
        replay_argv=[sys.executable, "-c", "print('replayed')"],
        signing_key="replay-secret",
    )
    manifest_path = tmp_path / EVIDENCE_MANIFEST_FILENAME

    plan = replay_manifest(manifest_path)
    assert plan["status"] == "dry_run"
    monkeypatch.delenv("OPENSPACE_EVIDENCE_REPLAY_ENABLED", raising=False)
    with pytest.raises(EvidenceManifestError, match="requires"):
        replay_manifest(manifest_path, execute=True, signing_key="replay-secret")

    monkeypatch.setenv("OPENSPACE_EVIDENCE_REPLAY_ENABLED", "1")
    result = replay_manifest(manifest_path, execute=True, signing_key="replay-secret")
    assert result["status"] == "completed"
    assert result["stdout"].strip() == "replayed"


def test_live_replay_requires_authenticated_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "input.txt").write_text("evidence", encoding="utf-8")
    create_run_manifest(
        tmp_path,
        run={"task_id": "task-1"},
        replay_argv=[sys.executable, "-c", "print('replayed')"],
    )
    monkeypatch.setenv("OPENSPACE_EVIDENCE_REPLAY_ENABLED", "1")
    with pytest.raises(EvidenceManifestError, match="verification failed"):
        replay_manifest(tmp_path / EVIDENCE_MANIFEST_FILENAME, execute=True)


def test_manifest_identity_detects_metadata_tampering(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("ok", encoding="utf-8")
    create_run_manifest(tmp_path, run={"task_id": "task-1", "status": "completed"})
    path = tmp_path / EVIDENCE_MANIFEST_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["run"]["status"] = "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_manifest(path)
    assert report["ok"] is False
    assert "manifest_id_mismatch" in report["errors"]


def test_verification_counts_only_matching_artifacts_and_validates_summary(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "data.txt"
    artifact.write_text("before", encoding="utf-8")
    create_run_manifest(tmp_path, run={"task_id": "task-1"})
    artifact.write_text("after", encoding="utf-8")

    report = verify_manifest(tmp_path / EVIDENCE_MANIFEST_FILENAME)
    assert report["verified_artifacts"] == 0
    assert "artifact_digest_mismatch:data.txt" in report["errors"]
    assert "artifact_size_mismatch:data.txt" in report["errors"]


def test_relative_output_path_is_rooted_in_evidence_directory(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("ok", encoding="utf-8")
    create_run_manifest(
        tmp_path,
        run={"task_id": "task-1"},
        output_path="manifests/run.json",
    )
    assert (tmp_path / "manifests" / "run.json").is_file()
