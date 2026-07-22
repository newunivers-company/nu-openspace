"""Optional, read-only NewUnivers routing and resource-generation adapters."""

from .adapter import (
    llm_route_diagnostics,
    newunivers_availability,
    resource_candidates,
    resource_health,
    resource_preflight,
)

__all__ = [
    "llm_route_diagnostics",
    "newunivers_availability",
    "resource_candidates",
    "resource_health",
    "resource_preflight",
]
