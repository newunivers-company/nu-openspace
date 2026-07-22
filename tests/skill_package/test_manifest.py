from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openspace.skill_package import (
    SKILL_PACKAGE_MANIFEST,
    SkillPackageError,
    build_skill_archive,
    build_skill_package_manifest,
    enforce_skill_package_policy,
    verify_skill_package,
)


def _skill(root: Path) -> Path:
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Safe demo skill\nlicense: MIT\n"
        "permissions:\n  - read\nallowed-tools:\n  - shell\n---\n# Demo\n",
        encoding="utf-8",
    )
    (root / "script.py").write_text("print('ok')\n", encoding="utf-8")
    return root


def test_signed_package_verifies_exact_inventory(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    manifest = build_skill_package_manifest(root, signing_key="secret", signing_key_id="test")
    assert manifest["capabilities"]["permissions"] == ["read"]
    report = verify_skill_package(root, signing_key="secret", require_signature=True)
    assert report["ok"] is True
    assert report["signature_status"] == "verified"

    (root / "undeclared.txt").write_text("extra", encoding="utf-8")
    report = verify_skill_package(root, signing_key="secret", require_signature=True)
    assert report["ok"] is False
    assert "file_undeclared:undeclared.txt" in report["errors"]


def test_package_detects_content_tampering(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    build_skill_package_manifest(root)
    (root / "script.py").write_text("print('tampered')\n", encoding="utf-8")
    report = verify_skill_package(root)
    assert "file_digest_mismatch:script.py" in report["errors"]
    assert report["verified_files"] == 1


def test_policy_can_require_manifest_and_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _skill(tmp_path / "demo")
    monkeypatch.setenv("OPENSPACE_REQUIRE_SKILL_PACKAGE_MANIFEST", "1")
    with pytest.raises(SkillPackageError, match="missing"):
        enforce_skill_package_policy(root)

    monkeypatch.setenv("OPENSPACE_REQUIRE_SIGNED_SKILL_PACKAGES", "1")
    monkeypatch.setenv("OPENSPACE_SKILL_PACKAGE_SIGNING_KEY", "secret")
    build_skill_package_manifest(root, signing_key="secret")
    assert enforce_skill_package_policy(root)["verified"] is True


def test_archive_is_deterministic_and_symlinks_are_refused(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    build_skill_package_manifest(root)
    first = build_skill_archive(root, tmp_path / "first.zip")
    second = build_skill_archive(root, tmp_path / "second.zip")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()

    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)
    with pytest.raises(SkillPackageError, match="symlink"):
        build_skill_package_manifest(root)


def test_package_detects_executable_mode_tampering(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    build_skill_package_manifest(root)
    script = root / "script.py"
    script.chmod(0o755)
    report = verify_skill_package(root)
    assert "file_mode_mismatch:script.py" in report["errors"]


def test_archive_output_must_be_outside_package(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    build_skill_package_manifest(root)
    with pytest.raises(SkillPackageError, match="outside"):
        build_skill_archive(root, root / "demo.zip")


def test_manifest_file_is_created(tmp_path: Path) -> None:
    root = _skill(tmp_path / "demo")
    build_skill_package_manifest(root)
    assert (root / SKILL_PACKAGE_MANIFEST).is_file()
