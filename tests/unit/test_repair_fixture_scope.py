"""随机声明只能降低 Mock 证据范围，且旧编码保持不变。"""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import OpenAIOptions
from agentgit.core.repair import NewRepairSearch
from agentgit.core.research import AgentConfig, BudgetLimits
from agentgit.repair.policy import repair_model_scope


def test_default_mock_and_provider_scope_cannot_be_relabelled_as_fixture():
    assert repair_model_scope(AgentConfig()) == "deterministic_fixture"
    assert repair_model_scope(AgentConfig(), "stochastic_fixture") == "stochastic_fixture"
    provider = AgentConfig(
        model="openai/gpt-5.4-mini-2026-03-17",
        openai=OpenAIOptions(),
        limits=BudgetLimits(max_cost_micro_usd=1_000_000),
    )
    assert repair_model_scope(provider) == "provider"
    with pytest.raises(AgentGitError) as error:
        repair_model_scope(provider, "stochastic_fixture")
    assert error.value.code == ErrorCode.FORBIDDEN


def test_new_request_preserves_old_encoding_and_only_accepts_conservative_scope():
    body = dict(
        failed_run_id=str(uuid4()),
        base_commit="a" * 64,
        validator_ref="b" * 64,
        targets=[{"schema_version": 1, "event_id": 1}],
    )
    request = NewRepairSearch.model_validate_json(json.dumps({"schema_version": 1, **body}))
    assert "fixture_scope" not in request.model_dump(mode="json")
    assert (
        NewRepairSearch.model_validate_json(
            json.dumps({"schema_version": 1, **body, "fixture_scope": "stochastic_fixture"})
        ).fixture_scope
        == "stochastic_fixture"
    )
    for invalid in ("provider", "deterministic_fixture", True, {}, 0):
        with pytest.raises(ValidationError):
            NewRepairSearch.model_validate_json(
                json.dumps({"schema_version": 1, **body, "fixture_scope": invalid})
            )
