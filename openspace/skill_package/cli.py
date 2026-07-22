"""CLI for building, validating, and archiving universal skill packages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .manifest import (
    SkillPackageError,
    build_skill_archive,
    build_skill_package_manifest,
    verify_skill_package,
)


def _key(env_name: str) -> str | None:
    return os.environ.get(env_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openspace-skill-package")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("skill_dir", type=Path)
    build.add_argument("--version", default="1.0.0")
    build.add_argument("--license", dest="license_expression")
    build.add_argument("--permission", action="append")
    build.add_argument("--tool", action="append")
    build.add_argument("--model", action="append")
    build.add_argument("--os", dest="operating_systems", action="append")
    build.add_argument("--signing-key-env", default="OPENSPACE_SKILL_PACKAGE_SIGNING_KEY")
    build.add_argument("--signing-key-id", default="local")

    verify = sub.add_parser("verify")
    verify.add_argument("skill_dir", type=Path)
    verify.add_argument("--signing-key-env", default="OPENSPACE_SKILL_PACKAGE_SIGNING_KEY")
    verify.add_argument("--require-signature", action="store_true")
    verify.add_argument("--require-license", action="store_true")

    pack = sub.add_parser("pack")
    pack.add_argument("skill_dir", type=Path)
    pack.add_argument("output", type=Path)
    pack.add_argument("--signing-key-env", default="OPENSPACE_SKILL_PACKAGE_SIGNING_KEY")
    pack.add_argument("--require-signature", action="store_true")
    pack.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            payload = build_skill_package_manifest(
                args.skill_dir,
                version=args.version,
                license_expression=args.license_expression,
                permissions=args.permission,
                tools=args.tool,
                models=args.model,
                operating_systems=args.operating_systems,
                signing_key=_key(args.signing_key_env),
                signing_key_id=args.signing_key_id,
            )
        elif args.command == "verify":
            payload = verify_skill_package(
                args.skill_dir,
                signing_key=_key(args.signing_key_env),
                require_signature=args.require_signature,
                require_declared_license=args.require_license,
            )
        else:
            archive = build_skill_archive(
                args.skill_dir,
                args.output,
                signing_key=_key(args.signing_key_env),
                require_signature=args.require_signature,
                overwrite=args.force,
            )
            payload = {"ok": True, "archive": str(archive)}
    except (SkillPackageError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
