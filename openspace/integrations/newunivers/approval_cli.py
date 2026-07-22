"""CLI for creating and inspecting signed NU resource approvals."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .governance import NuGovernanceError, create_resource_approval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openspace-nu-approval")
    parser.add_argument("output", type=Path)
    parser.add_argument("--candidate-id", action="append", required=True)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--params-json", default="{}")
    parser.add_argument("--media-json", default="{}")
    parser.add_argument("--max-cost", type=float, default=0.0)
    parser.add_argument("--cost-unit", default="local")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--max-uses", type=int, default=1)
    parser.add_argument("--expires-in", type=int, default=3600)
    parser.add_argument("--subject", default="operator")
    parser.add_argument("--signing-key-env", default="OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY")
    parser.add_argument("--signing-key-id", default="local")
    args = parser.parse_args(argv)
    try:
        key = os.environ.get(args.signing_key_env, "")
        payload = create_resource_approval(
            args.output,
            candidate_ids=args.candidate_id,
            prompt=args.prompt,
            params=json.loads(args.params_json),
            media=json.loads(args.media_json),
            max_cost=args.max_cost,
            cost_unit=args.cost_unit,
            allow_remote=args.allow_remote,
            max_uses=args.max_uses,
            expires_in_seconds=args.expires_in,
            subject=args.subject,
            signing_key=key,
            signing_key_id=args.signing_key_id,
        )
    except (NuGovernanceError, json.JSONDecodeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
