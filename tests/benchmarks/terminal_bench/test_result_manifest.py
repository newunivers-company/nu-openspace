import json
from pathlib import Path

import jsonschema

from benchmarks.terminal_bench.result_manifest import build_manifest, summarize_run


def _write_result(run_dir: Path, trial: str, task: str, reward: float) -> None:
    trial_dir = run_dir / trial
    trial_dir.mkdir(parents=True)
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "task_name": task,
                "verifier_result": {"rewards": {"reward": reward}},
            }
        ),
        encoding="utf-8",
    )


def test_run_summary_counts_rewards_and_has_stable_digest(tmp_path: Path) -> None:
    run = tmp_path / "cold"
    _write_result(run, "trial-a", "task-a", 1.0)
    _write_result(run, "trial-b", "task-b", 0.0)

    first = summarize_run(run)
    second = summarize_run(run)

    assert first["task_count"] == 2
    assert first["passed_count"] == 1
    assert first["pass_rate"] == 0.5
    assert first["result_json_tree_sha256"] == second["result_json_tree_sha256"]


def test_complete_manifest_summarizes_cold_and_warm(tmp_path: Path) -> None:
    cold = tmp_path / "cold"
    warm = tmp_path / "warm"
    _write_result(cold, "trial-a", "task-a", 0.0)
    _write_result(warm, "trial-a", "task-a", 1.0)

    manifest = build_manifest(
        cold=cold,
        warm=warm,
        model="test/model",
        dataset="terminal-bench@test",
        command="harbor run test",
    )

    assert manifest["status"] == "complete"
    assert manifest["reproducible"] is True
    assert manifest["cold"]["pass_rate"] == 0.0
    assert manifest["warm"]["pass_rate"] == 1.0


def test_published_claim_is_schema_valid_and_explicitly_incomplete() -> None:
    root = Path(__file__).resolve().parents[3]
    results = root / "benchmarks" / "terminal_bench" / "results"
    schema = json.loads((results / "manifest.schema.json").read_text(encoding="utf-8"))
    claim = json.loads((results / "published-v2-claim.json").read_text(encoding="utf-8"))

    jsonschema.validate(claim, schema)
    assert claim["reproducible"] is False
    assert claim["status"] == "historical_claim_artifacts_missing"
    assert claim["missing_artifacts"]
