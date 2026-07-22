"""Safe OpenSpace-facing adapters for the two NewUnivers libraries.

The functions in this module perform catalog inspection and deterministic
preflight checks only.  They deliberately do not expose either library's live
generation method.
"""

from __future__ import annotations

import importlib.util
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


_LLM_PACKAGE = "nu_llm_routing_lib"
_RESOURCE_PACKAGE = "nu_resource_gen_lib"
_ROUTER_ENV = "OPENSPACE_NU_LLM_ROUTER_CONFIG"
_SUPPORTED_LLM_USECASES = frozenset(
    {"agentic_tools", "coding", "structured_output", "summarization"}
)
_CATEGORY_ALIASES = {
    "image": "image_generation",
    "video": "video_generation",
    "vision": "vision_analysis",
    "audio": "audio_generation",
    "voice": "voice_audio",
    "music": "music_generation",
    "3d": "three_d_generation",
}


def _package_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def newunivers_availability() -> dict[str, bool]:
    """Return import availability without importing either optional package."""

    return {
        _LLM_PACKAGE: _package_available(_LLM_PACKAGE),
        _RESOURCE_PACKAGE: _package_available(_RESOURCE_PACKAGE),
    }


def _split_tags(tags: str) -> list[str]:
    return list(dict.fromkeys(part.strip() for part in tags.split(",") if part.strip()))


def _resolve_router_config(config_path: str = "") -> Path:
    explicit = config_path.strip() or os.getenv(_ROUTER_ENV, "").strip()
    if not explicit:
        explicit = os.getenv("NU_LLM_ROUTER_CONFIG", "").strip()
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())

    repo_root = Path(__file__).resolve().parents[3]
    candidates.extend(
        [
            repo_root.parent
            / "nu-llm-routing-lib"
            / "configs"
            / "byteplus.production.json",
            Path.cwd().parent
            / "nu-llm-routing-lib"
            / "configs"
            / "byteplus.production.json",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "NU LLM router config not found; set "
        f"{_ROUTER_ENV} to a reviewed router JSON file"
    )


def _compact_route_diagnostics(diagnostics: dict[str, Any], config: Path) -> dict[str, Any]:
    ranking = diagnostics.get("benchmark_ranking")
    ranked_providers: list[dict[str, Any]] = []
    if isinstance(ranking, dict):
        for item in ranking.get("providers", []):
            if not isinstance(item, dict):
                continue
            ranked_providers.append(
                {
                    "provider": item.get("provider"),
                    "group": item.get("group"),
                    "score": item.get("score"),
                    "failed_gates": item.get("failed_gates", []),
                }
            )

    provider_checks: list[dict[str, Any]] = []
    for item in diagnostics.get("provider_checks", []):
        if not isinstance(item, dict):
            continue
        provider_checks.append(
            {
                "provider": item.get("provider"),
                "allowed": item.get("allowed"),
                "reasons": item.get("reasons", []),
            }
        )

    return {
        "config_path": str(config),
        "config_revision": diagnostics.get("config_revision"),
        "matched_profile_id": diagnostics.get("matched_profile_id"),
        "initial_order": diagnostics.get("initial_order", []),
        "benchmark_order": diagnostics.get("benchmark_order", []),
        "final_order": diagnostics.get("final_order", []),
        "provider_checks": provider_checks,
        "benchmark_providers": ranked_providers,
        "terminal_action": diagnostics.get("terminal_action"),
        "terminal_reason": diagnostics.get("terminal_reason"),
        "status_probed": False,
        "model_called": False,
    }


def llm_route_diagnostics(
    *,
    usecase: str = "agentic_tools",
    config_path: str = "",
    hardware_profile: str = "",
    tags: str = "",
    gpu_free_vram_mib: int = 0,
    sandboxed_tools: bool = False,
) -> dict[str, Any]:
    """Resolve an NU LLM route without status probes or model calls."""

    normalized = usecase.strip().lower().replace("-", "_")
    if normalized not in _SUPPORTED_LLM_USECASES:
        choices = ", ".join(sorted(_SUPPORTED_LLM_USECASES))
        raise ValueError(f"unsupported usecase '{usecase}'; choose one of: {choices}")
    if gpu_free_vram_mib < 0:
        raise ValueError("gpu_free_vram_mib must be greater than or equal to zero")

    from nu_llm_routing_lib.api import ChatRequest, load_router_from_file
    from nu_llm_routing_lib.usecases import (
        agentic_tools_metadata,
        coding_metadata,
        structured_output_metadata,
        summarization_metadata,
    )

    common = {
        "hardware_profile": hardware_profile or None,
        "tags": _split_tags(tags) or None,
    }
    if normalized == "agentic_tools":
        metadata = agentic_tools_metadata(
            **common,
            sandboxed_tools=sandboxed_tools,
            gpu_free_vram_mib=gpu_free_vram_mib,
        )
    elif normalized == "coding":
        metadata = coding_metadata(
            **common,
            gpu_free_vram_mib=gpu_free_vram_mib,
        )
    elif normalized == "structured_output":
        metadata = structured_output_metadata(**common)
    else:
        metadata = summarization_metadata(**common)

    resolved_config = _resolve_router_config(config_path)
    router = load_router_from_file(resolved_config, load_dotenv=False)
    request = ChatRequest.from_text(
        "OpenSpace deterministic route inspection",
        metadata=metadata,
    )
    diagnostics = router.route_diagnostics(request, include_status=False)
    return _compact_route_diagnostics(diagnostics, resolved_config)


@lru_cache(maxsize=1)
def _resource_generator() -> Any:
    from nu_resource_gen_lib.api import ResourceGenerator

    return ResourceGenerator(record_ledger=False, archive_artifacts=False)


def _candidate_summary(generator: Any, spec: Any) -> dict[str, Any]:
    cost_rank, cost_tier = generator.cost_rank(spec)
    return {
        "candidate_id": spec.candidate_id,
        "provider": spec.provider,
        "category": spec.category,
        "adapter": spec.adapter,
        "model": spec.model,
        "cost": spec.cost,
        "cost_unit": spec.cost_unit,
        "cost_rank": cost_rank,
        "cost_tier": cost_tier,
        "credential_env": list(spec.credential_env),
        "deprecated": bool(spec.extras.get("deprecated")),
    }


def resource_candidates(
    *,
    category: str = "",
    provider: str = "",
    limit: int = 10,
    include_deprecated: bool = False,
) -> dict[str, Any]:
    """Return the free-first resource candidate catalog without execution."""

    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    requested_category = category.strip().lower()
    resolved_category = _CATEGORY_ALIASES.get(requested_category, requested_category)
    generator = _resource_generator()
    candidates = generator.recommend_candidates(
        category=resolved_category or None,
        provider=provider.strip().lower() or None,
        allow_paid_byteplus=False,
        include_deprecated=include_deprecated,
    )
    selected = candidates[:limit]
    return {
        "category": resolved_category or None,
        "requested_category": requested_category or None,
        "provider": provider.strip().lower() or None,
        "free_first": True,
        "paid_byteplus_allowed": False,
        "total_matches": len(candidates),
        "returned": len(selected),
        "candidates": [_candidate_summary(generator, spec) for spec in selected],
        "executed": False,
    }


def resource_health() -> dict[str, Any]:
    """Return credential/local-availability checks without a provider request."""

    return {
        "providers": _resource_generator().health(),
        "credential_or_local_checks_only": True,
        "network_probed": False,
        "executed": False,
    }


def _json_object(value: str, field_name: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    if len(value) > 64 * 1024:
        raise ValueError(f"{field_name} JSON exceeds the 64 KiB preflight limit")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def resource_preflight(
    *,
    candidate_id: str,
    prompt: str = "",
    params_json: str = "{}",
    media_json: str = "{}",
) -> dict[str, Any]:
    """Evaluate the default execution policy without calling a provider."""

    from nu_resource_gen_lib.api import ResourceRequest

    generator = _resource_generator()
    spec = generator.get_candidate(candidate_id.strip())
    request = ResourceRequest(
        prompt=prompt,
        params=_json_object(params_json, "params"),
        media=_json_object(media_json, "media"),
    )
    return {
        "candidate": _candidate_summary(generator, spec),
        "policy": generator.execution_policy_status(spec.candidate_id, request),
        "dry_run": True,
        "executed": False,
        "ledger_written": False,
    }
