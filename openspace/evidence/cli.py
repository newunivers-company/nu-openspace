"""Command line interface for Evidence Plane v3 manifests."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .manifest import (
    EvidenceManifestError,
    create_run_manifest,
    replay_manifest,
    verify_manifest,
)


def _key_from_env(name: str | None) -> str | None:
    return os.environ.get(name) if name else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openspace-evidence")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create a v3 manifest")
    create.add_argument("root", type=Path)
    create.add_argument("--task-id", required=True)
    create.add_argument("--session-id", default="")
    create.add_argument("--status", default="unknown")
    create.add_argument("--workspace-root", type=Path)
    create.add_argument("--signing-key-env", default="OPENSPACE_EVIDENCE_SIGNING_KEY")
    create.add_argument("--signing-key-id", default="local")
    create.add_argument("--replay-arg", action="append", default=[])

    verify = commands.add_parser("verify", help="verify a v3 manifest")
    verify.add_argument("manifest", type=Path)
    verify.add_argument("--signing-key-env", default="OPENSPACE_EVIDENCE_SIGNING_KEY")
    verify.add_argument("--require-signature", action="store_true")

    replay = commands.add_parser("replay", help="verify then plan/execute replay")
    replay.add_argument("manifest", type=Path)
    replay.add_argument("--execute", action="store_true")
    replay.add_argument("--signing-key-env", default="OPENSPACE_EVIDENCE_SIGNING_KEY")
    replay.add_argument("--require-signature", action="store_true")
    replay.add_argument("--timeout", type=float, default=600.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            payload = create_run_manifest(
                args.root,
                run={
                    "task_id": args.task_id,
                    "session_id": args.session_id,
                    "status": args.status,
                },
                workspace_root=args.workspace_root,
                replay_argv=args.replay_arg or None,
                signing_key=_key_from_env(args.signing_key_env),
                signing_key_id=args.signing_key_id,
            )
        elif args.command == "verify":
            payload = verify_manifest(
                args.manifest,
                signing_key=_key_from_env(args.signing_key_env),
                require_signature=args.require_signature,
            )
        else:
            payload = replay_manifest(
                args.manifest,
                execute=args.execute,
                signing_key=_key_from_env(args.signing_key_env),
                require_signature=args.require_signature,
                timeout_s=args.timeout,
            )
    except (EvidenceManifestError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", payload.get("status") != "failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
