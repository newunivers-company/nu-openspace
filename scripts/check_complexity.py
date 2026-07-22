#!/usr/bin/env python3
"""Fail on new extreme Python modules/functions while legacy hotspots shrink."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "openspace"
MAX_FILE_LINES = 4_000
MAX_CLASS_LINES = 2_700
DEFAULT_MAX_FUNCTION_LINES = 250

# Existing hotspots are debt baselines, not targets. They may shrink without
# updating this map, but they may not grow past the audited 2026-07-22 span.
FUNCTION_ALLOWANCES = {
    "openspace/agents/agent_tool.py:run_agent": 276,
    "openspace/agents/agent_tool.py:_arun": 282,
    "openspace/agents/turns/loop.py:run_grounding_turn": 815,
    "openspace/agents/turns/model_call_controller.py:call_model_with_recovery": 274,
    "openspace/agents/turns/model_call_controller.py:handle_model_response": 462,
    "openspace/agents/turns/tool_turn_controller.py:execute_tool_turn": 258,
    "openspace/application.py:__post_init__": 338,
    "openspace/cli/slash_commands.py:execute_slash_command": 567,
    "openspace/entrypoints/dashboard/server.py:create_app": 428,
    "openspace/entrypoints/dashboard/server.py:_build_workflow_trace": 547,
    "openspace/entrypoints/tui/controller.py:tui_mode": 439,
    "openspace/grounding/backends/shell/file_tools.py:_arun": 260,
    "openspace/llm/client.py:call_model": 421,
    "openspace/runtime/app.py:initialize_services": 692,
    "openspace/services/conversation/attachments.py:format_attachment_for_model": 353,
    "openspace/services/memory/dream.py:_execute_impl": 324,
    "openspace/skill_engine/evolver.py:_run_evolution_loop": 273,
    "openspace/tool_runtime/pipeline/execution.py:run_tool_use": 1_075,
    "openspace/tool_runtime/pipeline/execution.py:_handle_permission_ask": 336,
}


def main() -> int:
    failures: list[str] = []
    measured_files = 0
    measured_functions = 0
    largest_file = (0, "")
    largest_function = (0, "")

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "packaged" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        line_count = len(source.splitlines())
        measured_files += 1
        largest_file = max(largest_file, (line_count, relative))
        if line_count > MAX_FILE_LINES:
            failures.append(
                f"{relative}: {line_count} lines exceeds file limit {MAX_FILE_LINES}"
            )

        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                measured_functions += 1
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                key = f"{relative}:{node.name}"
                largest_function = max(largest_function, (span, key))
                allowance = FUNCTION_ALLOWANCES.get(
                    key,
                    DEFAULT_MAX_FUNCTION_LINES,
                )
                if span > allowance:
                    failures.append(
                        f"{key}: {span} lines exceeds function allowance {allowance}"
                    )
            elif isinstance(node, ast.ClassDef):
                span = (node.end_lineno or node.lineno) - node.lineno + 1
                if span > MAX_CLASS_LINES:
                    failures.append(
                        f"{relative}:{node.name}: {span} lines exceeds class limit "
                        f"{MAX_CLASS_LINES}"
                    )

    print(
        f"complexity: files={measured_files}, functions={measured_functions}, "
        f"largest_file={largest_file[1]}:{largest_file[0]}, "
        f"largest_function={largest_function[1]}:{largest_function[0]}"
    )
    if failures:
        print("Complexity budget violations:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
