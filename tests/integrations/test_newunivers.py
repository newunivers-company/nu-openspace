import importlib.util
from pathlib import Path

import pytest

from openspace.integrations.newunivers import (
    llm_route_diagnostics,
    newunivers_availability,
    resource_candidates,
    resource_health,
    resource_preflight,
)


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("nu_llm_routing_lib") is None
    or importlib.util.find_spec("nu_resource_gen_lib") is None,
    reason="optional NewUnivers sibling libraries are not installed",
)


def test_llm_route_is_deterministic_and_does_not_probe_or_call_models() -> None:
    config = (
        Path(__file__).resolve().parents[3]
        / "nu-llm-routing-lib"
        / "configs"
        / "byteplus.production.json"
    )

    result = llm_route_diagnostics(
        usecase="agentic_tools",
        config_path=str(config),
        gpu_free_vram_mib=32_000,
        sandboxed_tools=False,
    )

    assert result["matched_profile_id"] == "agentic_tools"
    assert result["final_order"]
    assert result["status_probed"] is False
    assert result["model_called"] is False


def test_resource_catalog_is_free_first_and_preflight_only() -> None:
    result = resource_candidates(category="image_generation", limit=5)

    assert result["executed"] is False
    assert result["paid_byteplus_allowed"] is False
    assert result["candidates"]
    ranks = [candidate["cost_rank"] for candidate in result["candidates"]]
    assert ranks == sorted(ranks)

    preflight = resource_preflight(
        candidate_id=result["candidates"][0]["candidate_id"],
        prompt="A harmless local test image",
    )
    assert preflight["dry_run"] is True
    assert preflight["executed"] is False
    assert preflight["ledger_written"] is False


def test_resource_catalog_accepts_human_friendly_category_aliases() -> None:
    vision = resource_candidates(category="vision", limit=3)

    assert vision["category"] == "vision_analysis"
    assert vision["candidates"]
    assert all(item["category"] == "vision_analysis" for item in vision["candidates"])


def test_health_is_local_or_credential_check_only() -> None:
    availability = newunivers_availability()
    health = resource_health()

    assert all(availability.values())
    assert health["providers"]
    assert health["network_probed"] is False
    assert health["executed"] is False
