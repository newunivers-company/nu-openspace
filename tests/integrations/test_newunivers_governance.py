from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from openspace.integrations.newunivers.governance import (
    NuExecutionLedger,
    NuGovernanceError,
    create_resource_approval,
    governed_resource_execute,
)


@dataclass
class _Spec:
    candidate_id: str = "local-test"
    provider: str = "comfyui"
    category: str = "image_generation"
    model: str = "test-model"
    cost: float = 0.0
    cost_unit: str = "local"
    extras: dict = field(
        default_factory=lambda: {
            "execution": "comfyui_pool",
            "quality_presets": {
                "presets": {"balanced": {"acceptance_gates": ["image_decode"]}}
            },
        }
    )


@dataclass
class _Result:
    candidate_id: str = "local-test"
    provider: str = "comfyui"
    model: str = "test-model"
    asset_uri: str = "/tmp/generated.png"
    status: str = "completed"
    cost: float = 0.0
    cost_unit: str = "local"


class _Generator:
    def __init__(self, spec: _Spec | None = None) -> None:
        self.spec = spec or _Spec()
        self.generate_calls = 0
        self.dry_run_calls = 0

    def get_candidate(self, candidate_id):
        assert candidate_id == self.spec.candidate_id
        return self.spec

    def execution_policy_status(self, candidate_id, request):
        return {"candidate_id": candidate_id, "blocked": False, "blockers": []}

    def record_dry_run(self, candidate_id, request):
        self.dry_run_calls += 1
        return {"written": True}

    def generate(self, candidate_id, request):
        self.generate_calls += 1
        return _Result()


def test_governed_execution_is_dry_run_by_default() -> None:
    generator = _Generator()
    result = governed_resource_execute(candidate_id="local-test", generator=generator)
    assert result["status"] == "planned"
    assert result["executed"] is False
    assert result["quality_gates"] == ["image_decode"]
    assert generator.generate_calls == 0
    assert generator.dry_run_calls == 1


def test_live_execution_requires_capability_and_consumes_signed_approval_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_path = tmp_path / "approval.json"
    create_resource_approval(
        approval_path,
        candidate_ids=["local-test"],
        prompt="approved prompt",
        max_cost=0,
        cost_unit="local",
        signing_key="approval-secret",
    )
    generator = _Generator()
    ledger = NuExecutionLedger(tmp_path / "ledger.db")

    with pytest.raises(NuGovernanceError, match="disabled"):
        governed_resource_execute(
            candidate_id="local-test",
            prompt="approved prompt",
            approval_path=str(approval_path),
            dry_run=False,
            generator=generator,
            ledger=ledger,
        )

    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_CAPABILITY_TOKEN", "long-capability-token")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY", "approval-secret")
    result = governed_resource_execute(
        candidate_id="local-test",
        prompt="approved prompt",
        approval_path=str(approval_path),
        dry_run=False,
        generator=generator,
        ledger=ledger,
    )
    assert result["status"] == "completed"
    assert result["executed"] is True
    assert generator.generate_calls == 1

    with pytest.raises(NuGovernanceError, match="exhausted"):
        governed_resource_execute(
            candidate_id="local-test",
            prompt="approved prompt",
            approval_path=str(approval_path),
            dry_run=False,
            generator=generator,
            ledger=ledger,
        )


def test_approval_is_bound_to_request_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    approval_path = tmp_path / "approval.json"
    create_resource_approval(
        approval_path,
        candidate_ids=["local-test"],
        prompt="approved",
        signing_key="approval-secret",
    )
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_CAPABILITY_TOKEN", "long-capability-token")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY", "approval-secret")
    with pytest.raises(NuGovernanceError, match="request_digest_mismatch"):
        governed_resource_execute(
            candidate_id="local-test",
            prompt="changed",
            approval_path=str(approval_path),
            dry_run=False,
            generator=_Generator(),
            ledger=NuExecutionLedger(tmp_path / "ledger.db"),
        )


def test_approval_rejects_non_finite_budget(tmp_path: Path) -> None:
    with pytest.raises(NuGovernanceError, match="finite"):
        create_resource_approval(
            tmp_path / "approval.json",
            candidate_ids=["local-test"],
            prompt="approved",
            max_cost=float("inf"),
            signing_key="approval-secret",
        )


def test_unknown_provider_is_not_treated_as_local_by_cost_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approval_path = tmp_path / "approval.json"
    create_resource_approval(
        approval_path,
        candidate_ids=["local-test"],
        prompt="approved",
        max_cost=0,
        cost_unit="local",
        allow_remote=False,
        signing_key="approval-secret",
    )
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_CAPABILITY_TOKEN", "long-capability-token")
    monkeypatch.setenv("OPENSPACE_NU_RESOURCE_APPROVAL_SIGNING_KEY", "approval-secret")
    remote_spec = _Spec(provider="unknown-api", extras={"execution": "provider_api"})
    with pytest.raises(NuGovernanceError, match="remote_execution_not_approved"):
        governed_resource_execute(
            candidate_id="local-test",
            prompt="approved",
            approval_path=str(approval_path),
            dry_run=False,
            generator=_Generator(remote_spec),
            ledger=NuExecutionLedger(tmp_path / "ledger.db"),
        )
