"""Read-only meta tools backed by optional NewUnivers libraries."""

from __future__ import annotations

import importlib.util
import json
from typing import Any

from ..types import ToolResult, ToolStatus
from .tool import _BaseMetaTool


def _success(value: dict[str, Any]) -> ToolResult:
    return ToolResult(
        status=ToolStatus.SUCCESS,
        content=json.dumps(value, ensure_ascii=False, separators=(",", ":")),
    )


class NuLlmRouteTool(_BaseMetaTool):
    _name = "nu_llm_route"
    _description = (
        "Inspect the deterministic NewUnivers LLM provider route. "
        "Never probes provider status and never calls a model."
    )
    parameter_descriptions = {
        "usecase": "agentic_tools, coding, structured_output, or summarization",
        "config_path": "Reviewed router JSON; empty uses configured/default sibling path",
        "gpu_free_vram_mib": "Deterministic free-VRAM input; zero disables GPU-only routes",
        "sandboxed_tools": "Whether this planned agent workload runs tools in a sandbox",
    }

    async def _arun(
        self,
        usecase: str = "agentic_tools",
        config_path: str = "",
        hardware_profile: str = "",
        tags: str = "",
        gpu_free_vram_mib: int = 0,
        sandboxed_tools: bool = False,
    ) -> ToolResult:
        from openspace.integrations.newunivers import llm_route_diagnostics

        return _success(
            llm_route_diagnostics(
                usecase=usecase,
                config_path=config_path,
                hardware_profile=hardware_profile,
                tags=tags,
                gpu_free_vram_mib=gpu_free_vram_mib,
                sandboxed_tools=sandboxed_tools,
            )
        )


class NuResourceCatalogTool(_BaseMetaTool):
    _name = "nu_resource_catalog"
    _description = (
        "List NewUnivers image, video, audio, or vision candidates in free-first order. "
        "Does not generate an asset."
    )

    async def _arun(
        self,
        category: str = "",
        provider: str = "",
        limit: int = 10,
        include_deprecated: bool = False,
    ) -> ToolResult:
        from openspace.integrations.newunivers import resource_candidates

        return _success(
            resource_candidates(
                category=category,
                provider=provider,
                limit=limit,
                include_deprecated=include_deprecated,
            )
        )


class NuResourceHealthTool(_BaseMetaTool):
    _name = "nu_resource_health"
    _description = (
        "Check NewUnivers provider credential or local-runtime availability. "
        "Does not perform a network or generation request."
    )

    async def _arun(self) -> ToolResult:
        from openspace.integrations.newunivers import resource_health

        return _success(resource_health())


class NuResourcePreflightTool(_BaseMetaTool):
    _name = "nu_resource_preflight"
    _description = (
        "Evaluate NewUnivers billing, license, consent, and media execution policy "
        "for one candidate. This is a dry run and never calls a provider."
    )

    async def _arun(
        self,
        candidate_id: str,
        prompt: str = "",
        params_json: str = "{}",
        media_json: str = "{}",
    ) -> ToolResult:
        from openspace.integrations.newunivers import resource_preflight

        return _success(
            resource_preflight(
                candidate_id=candidate_id,
                prompt=prompt,
                params_json=params_json,
                media_json=media_json,
            )
        )


def optional_meta_tool_classes() -> list[type[_BaseMetaTool]]:
    """Discover integrations without importing optional provider packages."""

    classes: list[type[_BaseMetaTool]] = []
    if importlib.util.find_spec("nu_llm_routing_lib") is not None:
        classes.append(NuLlmRouteTool)
    if importlib.util.find_spec("nu_resource_gen_lib") is not None:
        classes.extend(
            [NuResourceCatalogTool, NuResourceHealthTool, NuResourcePreflightTool]
        )
    return classes
