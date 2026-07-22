import importlib.util
import json
from types import SimpleNamespace

import pytest

from openspace.grounding.core.meta.newunivers_tools import (
    NuResourceCatalogTool,
    optional_meta_tool_classes,
)
from openspace.grounding.core.types import ToolStatus


def test_optional_tools_follow_installed_packages() -> None:
    names = {tool._name for tool in optional_meta_tool_classes()}

    assert ("nu_llm_route" in names) is (
        importlib.util.find_spec("nu_llm_routing_lib") is not None
    )
    assert ("nu_resource_catalog" in names) is (
        importlib.util.find_spec("nu_resource_gen_lib") is not None
    )


@pytest.mark.asyncio
async def test_resource_meta_tool_returns_compact_nonexecuting_result() -> None:
    if importlib.util.find_spec("nu_resource_gen_lib") is None:
        pytest.skip("optional NewUnivers resource library is not installed")
    tool = NuResourceCatalogTool(SimpleNamespace())

    result = await tool._arun(category="image_generation", limit=2)
    content = json.loads(result.content)

    assert result.status == ToolStatus.SUCCESS
    assert content["returned"] <= 2
    assert content["executed"] is False
