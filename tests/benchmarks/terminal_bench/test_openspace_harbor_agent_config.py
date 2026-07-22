import asyncio
import inspect
import json
import shutil

import pytest

from benchmarks.terminal_bench import openspace_harbor_agent
from benchmarks.terminal_bench import run_benchmark


def test_harbor_agent_exposes_task_tools_by_default():
    assert "TaskGet" in openspace_harbor_agent._DEFAULT_ACTIVE_TOOL_NAMES
    assert "TaskList" in openspace_harbor_agent._DEFAULT_ACTIVE_TOOL_NAMES


def test_harbor_agent_default_backend_scope_includes_meta_tools():
    signature = inspect.signature(openspace_harbor_agent.OpenSpaceHarborAgent)

    assert signature.parameters["backend_scope"].default == "shell,meta"


def test_terminal_bench_prompt_mentions_blocking_task_get():
    preamble = openspace_harbor_agent._TERMINAL_BENCH_PREAMBLE

    assert "TaskGet with block=true and timeout=600000" in preamble


def test_visible_test_context_is_disabled_by_default(tmp_path):
    signature = inspect.signature(openspace_harbor_agent.OpenSpaceHarborAgent)

    assert signature.parameters["visible_test_context_enabled"].default is False
    assert signature.parameters["visible_test_context_max_chars"].default == 12000
    assert signature.parameters["bench_stop_after_checker_pass_iterations"].default == 2
    assert signature.parameters["replay_success_bootstrap_enabled"].default is False
    assert signature.parameters["replay_success_bootstrap_skip_agent"].default is True
    assert (
        signature.parameters["evolution_allow_single_observation_capture"].default
        is True
    )
    assert (
        signature.parameters[
            "skill_trust_promotion_min_independent_successes"
        ].default
        == 2
    )
    assert signature.parameters["llm_max_tokens"].default == 4096
    assert signature.parameters["execution_analyzer_max_tokens"].default == 8192
    assert signature.parameters["skill_evolver_max_tokens"].default == 8192
    assert (
        signature.parameters[
            "evolution_capture_semantic_validation_enabled"
        ].default
        is True
    )
    assert (
        signature.parameters[
            "evolution_capture_semantic_validation_max_tokens"
        ].default
        == 2048
    )

    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=tmp_path / "agent",
        model_name="openrouter/tencent/hy3:free",
    )
    assert agent._llm_max_tokens == 4096
    assert agent._execution_analyzer_max_tokens == 8192
    assert agent._skill_evolver_max_tokens == 8192
    assert agent._evolution_capture_semantic_validation_enabled is True
    assert agent._evolution_capture_semantic_validation_max_tokens == 2048


def test_visible_test_context_collects_checker_paths():
    script = openspace_harbor_agent._VISIBLE_TEST_CONTEXT_SCRIPT

    assert "/tests" in script
    assert "/app/check.py" in script
    assert "hidden cases" in script


def test_submission_preamble_excludes_verifier_owned_tests():
    preamble = openspace_harbor_agent._TERMINAL_BENCH_PREAMBLE

    assert "Do not inspect verifier-owned paths such as /tests" in preamble
    assert "python -m pytest /tests" not in preamble
    assert "do not ignore a readable /tests" not in preamble


def test_terminal_bench_preamble_preserves_failed_checks_and_original_inputs():
    preamble = openspace_harbor_agent._TERMINAL_BENCH_PREAMBLE

    assert "Do not weaken its assertion" in preamble
    assert "reproduce the real mechanism" in preamble
    assert "copy every original input and related sidecar file" in preamble
    assert "destructive experiments only on copies" in preamble


def test_visible_acceptance_prompt_extracts_expected_values():
    targets = openspace_harbor_agent._extract_acceptance_targets(
        "Expected G_peak values: x0=1580.3, gamma=9.06, "
        "A=8382.69, offset=5561.03. Got: x0=1660"
    )
    prompt = openspace_harbor_agent._build_visible_acceptance_prompt(targets)

    assert prompt is not None
    assert "Visible checker acceptance targets" in prompt
    assert "G: x0=1580.3, gamma=9.06" in prompt


def test_success_replay_notes_extract_successful_tool_calls(tmp_path):
    seed_trial = tmp_path / "regex-chess__abc123"
    (seed_trial / "verifier").mkdir(parents=True)
    (seed_trial / "verifier" / "reward.txt").write_text("1\n", encoding="utf-8")
    recording_dir = seed_trial / "agent" / "recordings" / "task_1"
    recording_dir.mkdir(parents=True)
    record = {
        "delta_messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "bash",
                            "arguments": json.dumps(
                                {
                                    "command": (
                                        "cat > /app/re.json <<'EOF'\n"
                                        "[[\"a\", \"b\"]]\nEOF"
                                    )
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "Wrote /app/re.json",
                "_meta": {"status": "success"},
            },
        ]
    }
    (recording_dir / "conversations.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )

    notes = openspace_harbor_agent._extract_success_replay_notes(seed_trial)

    assert "Previous external reward: 1" in notes
    assert "cat > /app/re.json" in notes


def test_success_replay_bootstrap_prompt_is_action_oriented():
    prompt = openspace_harbor_agent._build_success_replay_bootstrap_prompt(
        task_slug="regex-chess",
        success_notes=(
            "Previous external reward: 1\n"
            "### Snippet 1\n"
            "Shell command:\n"
            "cat > /app/re.json <<'EOF'\n[]\nEOF"
        ),
    )

    assert prompt is not None
    assert "first execution plan" in prompt
    assert "cat > /app/re.json" in prompt


def test_success_replay_shell_script_extracts_constructive_steps():
    notes = "\n".join(
        (
            "Previous external reward: 1",
            "",
            "### Snippet 1",
            "Shell command:",
            "cat > /app/re.json <<'EOF'",
            "[]",
            "EOF",
            "",
            "### Snippet 2",
            "Edit file: /app/build.py",
            "Old string:",
            "out.add(short_fen)",
            "New string:",
            "out.add(full_fen)",
        )
    )

    script = openspace_harbor_agent._build_success_replay_shell_script(notes)

    assert script is not None
    assert "cat > /app/re.json" in script
    assert "out.add(short_fen)" in script
    assert "out.add(full_fen)" in script


def test_success_replay_notes_require_success_reward(tmp_path):
    seed_trial = tmp_path / "regex-chess__abc123"
    (seed_trial / "verifier").mkdir(parents=True)
    (seed_trial / "verifier" / "reward.txt").write_text("0\n", encoding="utf-8")
    recording_dir = seed_trial / "agent" / "recordings" / "task_1"
    recording_dir.mkdir(parents=True)
    (recording_dir / "conversations.jsonl").write_text(
        json.dumps({"delta_messages": []}) + "\n",
        encoding="utf-8",
    )

    assert openspace_harbor_agent._extract_success_replay_notes(seed_trial) == ""


def test_replay_feedback_skill_includes_success_notes(tmp_path):
    seed_trial = tmp_path / "regex-chess__abc123"
    (seed_trial / "verifier").mkdir(parents=True)
    (seed_trial / "verifier" / "reward.txt").write_text("1\n", encoding="utf-8")
    recording_dir = seed_trial / "agent" / "recordings" / "task_1"
    recording_dir.mkdir(parents=True)
    record = {
        "delta_messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "edit",
                            "arguments": json.dumps(
                                {
                                    "file_path": "/app/build.py",
                                    "old_string": "out.add(short_fen)",
                                    "new_string": "out.add(full_fen)",
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "updated",
                "_meta": {"status": "success"},
            },
        ]
    }
    (recording_dir / "conversations.jsonl").write_text(
        json.dumps(record) + "\n",
        encoding="utf-8",
    )
    current_logs = tmp_path / "current" / "regex-chess__xyz789" / "agent"
    current_logs.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=current_logs,
        model_name="openrouter/tencent/hy3:free",
    )

    skill = agent._build_replay_feedback_skill(seed_trial)
    assert skill is not None
    _, skill_dir = skill
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(skill_dir.parent, ignore_errors=True)

    assert "Successful Replay Tool Snippets" in text
    assert "out.add(full_fen)" in text


def test_agent_setup_prefers_package_manager_python_before_uv():
    source = inspect.getsource(openspace_harbor_agent.OpenSpaceHarborAgent.setup)

    assert "install_python312_with_package_manager" in source
    assert "python3-venv" in source
    assert "run_with_retries sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'" in source


def test_visible_test_context_metadata_is_not_written_to_cli_config():
    source = inspect.getsource(openspace_harbor_agent.OpenSpaceHarborAgent.run)

    assert '"visible_test_context_enabled"' not in source
    assert '"visible_test_context_chars"' not in source


def test_run_places_success_replay_before_visible_context():
    source = inspect.getsource(openspace_harbor_agent.OpenSpaceHarborAgent.run)

    success_index = source.index("if self._replay_seed_success_bootstrap_prompt")
    visible_index = source.index("if visible_acceptance_prompt")

    assert success_index < visible_index


def test_run_can_skip_agent_after_success_bootstrap():
    source = inspect.getsource(openspace_harbor_agent.OpenSpaceHarborAgent.run)

    assert "replay bootstrap succeeded; skipping LLM agent loop" in source
    assert "_replay_seed_success_bootstrap_succeeded" in source


def test_default_post_execution_timeout_is_benchmark_bounded():
    assert (
        openspace_harbor_agent._default_post_execution_timeout_s(
            evolution_enabled=True,
            final_drain_timeout_s=180,
            llm_timeout_sec=300,
        )
        == 240
    )
    assert (
        openspace_harbor_agent._default_post_execution_timeout_s(
            evolution_enabled=False,
            final_drain_timeout_s=180,
            llm_timeout_sec=300,
        )
        == 120
    )


def test_post_execution_is_disabled_for_score_only_runs(tmp_path):
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=tmp_path / "score-only",
        model_name="openrouter/tencent/hy3:free",
        evolution_enabled=False,
    )

    assert agent._post_execution_mode == "disabled"


def test_post_execution_defaults_inline_with_evolution_and_allows_override(tmp_path):
    evolving = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=tmp_path / "evolving",
        model_name="openrouter/tencent/hy3:free",
        evolution_enabled=True,
    )
    diagnostic = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=tmp_path / "diagnostic",
        model_name="openrouter/tencent/hy3:free",
        evolution_enabled=False,
        post_execution_mode="inline",
    )

    assert evolving._post_execution_mode == "inline"
    assert diagnostic._post_execution_mode == "inline"


def test_post_execution_mode_rejects_unknown_value():
    with pytest.raises(ValueError, match="post_execution_mode"):
        openspace_harbor_agent._resolve_post_execution_mode(
            "later",
            evolution_enabled=False,
        )


def test_run_writes_post_execution_mode_and_analyzer_start_policy():
    source = inspect.getsource(openspace_harbor_agent.OpenSpaceHarborAgent.run)

    assert '"post_execution_mode": self._post_execution_mode' in source
    assert '"execution_analysis_sync_start"' in source


def test_terminal_bench_launcher_defaults_to_meta_backend():
    args = run_benchmark.parse_args(
        ["--task", "fix-git", "--model", "tencent/hy3:free", "--dry-run"]
    )

    assert args.backend_scope == "shell,meta"
    command = run_benchmark.build_harbor_command(args)
    backend_index = command.index("backend_scope=shell,meta")
    assert command[backend_index - 1] == "--agent-kwarg"


def test_terminal_bench_launcher_scales_agent_setup_timeout_to_install_timeout():
    args = run_benchmark.parse_args(
        ["--task", "fix-git", "--model", "tencent/hy3:free", "--dry-run"]
    )

    command = run_benchmark.build_harbor_command(args)
    setup_index = command.index("--agent-setup-timeout-multiplier")

    assert command[setup_index + 1] == "11.0"


def test_terminal_bench_launcher_respects_explicit_agent_setup_timeout():
    args = run_benchmark.parse_args(
        [
            "--task",
            "fix-git",
            "--model",
            "tencent/hy3:free",
            "--agent-setup-timeout-multiplier",
            "4",
            "--dry-run",
        ]
    )

    command = run_benchmark.build_harbor_command(args)
    setup_index = command.index("--agent-setup-timeout-multiplier")

    assert command[setup_index + 1] == "4.0"


def test_terminal_bench_dry_run_does_not_require_credentials(monkeypatch, capsys):
    for name in (
        "OPENSPACE_LLM_API_KEY",
        "OPENROUTER_API_KEY",
        "OR_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    exit_code = run_benchmark.cli(
        [
            "--task",
            "fix-git",
            "--model",
            "openrouter/test/model",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert "harbor run" in capsys.readouterr().out


def test_replay_seed_without_artifacts_falls_back_to_cold_start(tmp_path):
    seed_trial = tmp_path / "seed" / "fix-git__abc123"
    (seed_trial / "agent").mkdir(parents=True)
    current_logs = tmp_path / "current" / "fix-git__xyz789" / "agent"
    current_logs.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=current_logs,
        model_name="openrouter/tencent/hy3:free",
        replay_seed_run_dir=str(tmp_path / "seed"),
    )

    metadata = asyncio.run(agent._upload_replay_seed_artifacts(environment=None))

    assert metadata["replay_seed_trial_found"] is True
    assert metadata["replay_seed_missing_artifacts"] is True
    assert metadata["replay_seed_evidence_uploaded"] is False


def test_missing_matching_replay_seed_trial_falls_back_to_cold_start(tmp_path):
    (tmp_path / "seed" / "other-task__abc123" / "agent").mkdir(parents=True)
    current_logs = tmp_path / "current" / "fix-git__xyz789" / "agent"
    current_logs.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=current_logs,
        model_name="openrouter/tencent/hy3:free",
        replay_seed_run_dir=str(tmp_path / "seed"),
    )

    metadata = asyncio.run(agent._upload_replay_seed_artifacts(environment=None))

    assert metadata["replay_seed_trial_found"] is False
    assert metadata["replay_seed_missing_artifacts"] is True
    assert "No matching replay seed trial" in metadata["replay_seed_missing_reason"]


def test_terminal_bench_agent_exports_checker_pass_stop_env(tmp_path):
    logs_dir = tmp_path / "trial__abc" / "agent"
    logs_dir.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=logs_dir,
        model_name="openrouter/tencent/hy3:free",
        api_key="test-only-key",
        bench_stop_after_checker_pass_iterations=1,
    )

    env = agent._env()

    assert env["OPENSPACE_BENCH_STOP_AFTER_CHECKER_PASS_ITERATIONS"] == "1"


def test_benchmark_stop_is_allowed_for_external_verifier(tmp_path):
    logs_dir = tmp_path / "trial__abc" / "agent"
    logs_dir.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=logs_dir,
        model_name="openrouter/tencent/hy3:free",
    )
    output = (
        "Task failed: best current artifact should be scored externally\n"
        "Status:          BENCH_CHECKER_PASS_BUDGET\n"
        "Grounding Agent: Execution completed: bench_checker_pass_budget"
    )

    assert agent._openspace_internal_failure(output, "") is True
    assert agent._openspace_benchmark_stop(output, "") is True


def test_plain_task_failure_is_not_benchmark_stop(tmp_path):
    logs_dir = tmp_path / "trial__abc" / "agent"
    logs_dir.mkdir(parents=True)
    agent = openspace_harbor_agent.OpenSpaceHarborAgent(
        logs_dir=logs_dir,
        model_name="openrouter/tencent/hy3:free",
    )

    assert agent._openspace_internal_failure("Task failed: nope", "") is True
    assert agent._openspace_benchmark_stop("Task failed: nope", "") is False
