import pytest

from openspace.application import OpenSpaceConfig
from openspace.runtime.app import resolve_grounding_max_iterations


def test_default_config_inherits_agent_configuration() -> None:
    config = OpenSpaceConfig()

    assert config.grounding_max_iterations is None
    assert resolve_grounding_max_iterations(
        config.grounding_max_iterations,
        {"max_iterations": 30},
    ) == 30


def test_explicit_twenty_is_not_treated_as_default_sentinel() -> None:
    assert resolve_grounding_max_iterations(20, {"max_iterations": 30}) == 20


@pytest.mark.parametrize("value", [0, -1, True, "invalid"])
def test_invalid_iteration_values_fail_closed(value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        resolve_grounding_max_iterations(value, {"max_iterations": 30})  # type: ignore[arg-type]
