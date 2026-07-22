# Terminal-Bench / Harbor

OpenSpace runs Terminal-Bench 2.1 through Harbor. Install the locked benchmark
extra and verify the harness contract before using credentials:

```bash
uv sync --locked --extra dev --extra benchmark
uv run pytest -q tests/benchmarks/terminal_bench
uv run python -m benchmarks.terminal_bench --sample smoke --dry-run
```

Run the cold and warm experiments with the same model, task set, iteration
budget, and Harbor version. Warm replay requires one attempt per task:

```bash
uv run python -m benchmarks.terminal_bench --sample full --job-name cold-v2
uv run python -m benchmarks.terminal_bench --sample full --job-name warm-v2 \
  --replay-from-run benchmarks/terminal_bench/runs/cold-v2
```

The runner requires Docker and model credentials. Run directories are ignored
because they can contain large logs and model output; archive them in controlled
storage before removing the local copies.

Generate a portable summary manifest after both runs:

```bash
uv run python -m benchmarks.terminal_bench.result_manifest \
  --cold benchmarks/terminal_bench/runs/cold-v2 \
  --warm benchmarks/terminal_bench/runs/warm-v2 \
  --model openrouter/provider/model \
  --command "python -m benchmarks.terminal_bench --sample full" \
  --output benchmark-result.json
```

The manifest records task/pass counts and a deterministic digest over every
Harbor `result.json`. Publish the manifest together with the raw run archives;
the digest alone cannot reproduce or audit model behavior.

## Published result artifact status

The README chart reports 65.2% Cold and 78.7% Warm across 89 tasks. The exact
commands, model identifier, commit, Harbor version, per-task `result.json`
files, and raw run archives were not committed. The claim is therefore marked
`historical_claim_artifacts_missing` in
[`results/published-v2-claim.json`](results/published-v2-claim.json) and must not
be treated as a reproducible benchmark result. A replacement result is complete
only when it validates against `results/manifest.schema.json` and its referenced
raw archives are available.
