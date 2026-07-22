"""Create an auditable summary manifest from cold and warm Harbor runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_result(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Harbor result must be a JSON object: {path}")
    return value


def _reward(result: dict[str, Any]) -> float | None:
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    value = rewards.get("reward")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def summarize_run(run_dir: Path) -> dict[str, Any]:
    resolved = run_dir.expanduser().resolve()
    result_paths = sorted(resolved.glob("*/result.json"))
    if not result_paths:
        raise ValueError(f"no Harbor trial result.json files found under {resolved}")

    digest = hashlib.sha256()
    passed = 0
    scored = 0
    tasks: list[str] = []
    for path in result_paths:
        relative = path.relative_to(resolved).as_posix()
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        result = _load_result(path)
        tasks.append(str(result.get("task_name") or path.parent.name))
        reward = _reward(result)
        if reward is not None:
            scored += 1
            passed += int(reward == 1.0)

    return {
        "run_dir": str(resolved),
        "task_count": len(result_paths),
        "scored_count": scored,
        "passed_count": passed,
        "pass_rate": passed / scored if scored else None,
        "result_json_tree_sha256": digest.hexdigest(),
        "tasks": tasks,
    }


def _git_revision() -> dict[str, Any]:
    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    try:
        return {
            "commit": run("rev-parse", "HEAD"),
            "dirty": bool(run("status", "--porcelain")),
        }
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unknown", "dirty": None}


def build_manifest(
    *,
    cold: Path,
    warm: Path,
    model: str,
    dataset: str,
    command: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "complete",
        "reproducible": True,
        "benchmark": "terminal-bench",
        "dataset": dataset,
        "model": model,
        "command": command,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": _git_revision(),
        "cold": summarize_run(cold),
        "warm": summarize_run(warm),
        "required_artifacts": [
            "cold Harbor run archive",
            "warm Harbor run archive",
            "this manifest",
        ],
        "missing_artifacts": [],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold", type=Path, required=True)
    parser.add_argument("--warm", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--dataset", default="terminal-bench@2.1")
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_manifest(
        cold=args.cold,
        warm=args.warm,
        model=args.model,
        dataset=args.dataset,
        command=args.command,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
