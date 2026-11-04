"""计划模板与审阅效果的严格编码；纯向量不代表实际批准。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.repair_reviews import DecideRepairReview, RepairReviewDecision
from agentgit.core.research import AgentConfig, Plan, PlanNode, evolve


def test_old_config_encoding_omits_optional_template_and_strict_roundtrip_is_unchanged():
    original = AgentConfig()
    assert "plan_template" not in original.model_dump(mode="json")
    assert parse_contract(encode(original.model_dump(mode="json")), AgentConfig) == original
    plan = Plan(nodes=(PlanNode(key="report", kind="write_report"),))
    configured = evolve(original, plan_template=plan)
    assert parse_contract(encode(configured.model_dump(mode="json")), AgentConfig) == configured


@pytest.mark.parametrize("kind", ["code_exec", "terminal_exec", "read"])
def test_template_cannot_add_unapproved_tools(kind):
    node = PlanNode(key="tool", kind=kind, source_slot=0 if kind == "read" else None)
    with pytest.raises(ValidationError):
        AgentConfig(tools=("artifact_write",), plan_template=Plan(nodes=(node,)))


def decision():
    return RepairReviewDecision(
        proposal_id=str(uuid4()),
        review_id=str(uuid4()),
        proposal_ref="a" * 64,
        ticket_ref="b" * 64,
        expected_config_version=8,
        decision="approve",
        actor="fixture",
        source_config_id=str(uuid4()),
        source_config_version=1,
        new_config_id=str(uuid4()),
        new_config_version=9,
        saved_experiment_id=None,
        lock_ref="c" * 64,
        verification_ref="d" * 64,
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(new_config_version=8),
        dict(new_config_id=None),
        dict(decision="reject"),
        dict(decision="save_experiment"),
        dict(saved_experiment_id=str(uuid4())),
    ],
)
def test_review_decision_cannot_disguise_or_expand_its_effect(change):
    with pytest.raises(ValidationError):
        evolve(decision(), **change)


def test_review_request_cannot_upload_config_verdict_or_execution():
    request = DecideRepairReview(
        review_id=str(uuid4()), proposal_ref="a" * 64, expected_config_version=1, decision="approve"
    )
    for field in ("after", "passed", "config_id", "execute"):
        with pytest.raises(AgentGitError):
            parse_contract(
                encode({**request.model_dump(mode="json"), field: True}), DecideRepairReview
            )
