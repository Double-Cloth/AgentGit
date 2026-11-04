"""审阅版本、并发、幂等和事务中断均在实际封存六加四之后验证。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DBAPIError
from test_repair_adoption import counts
from test_repair_confirmation import drive_confirmation, setup_confirmation

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair_reviews import DecideRepairReview
from agentgit.core.research import evolve
from agentgit.services.repair_reviews import TABLES, RepairReviewService

pytestmark = pytest.mark.integration


def confirmed(history, tmp_path, policy="pass"):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path, policy=policy)
    result = drive_confirmation(history, research, repo, policy=policy)
    assert result["status"] == ("verified_under_fixture" if policy == "pass" else "rejected")
    service = RepairReviewService(research)
    proposal = service.create(repo, study, actor="fixture", key="proposal")
    identity = UUID(proposal["proposal_id"])
    ticket = service.begin(repo, identity, actor="fixture", key="review")
    request = DecideRepairReview(
        review_id=ticket["review_id"],
        proposal_ref=proposal["proposal_ref"],
        expected_config_version=ticket["ticket"]["expected_config_version"],
        decision="approve",
    )
    return repo, study, research, service, identity, proposal, request


def test_pending_validation_cannot_create_recommendation_from_screen_result(history, tmp_path):
    repo, study, research, _, _ = setup_confirmation(history, tmp_path)
    before = counts(history)
    with pytest.raises(AgentGitError) as error:
        RepairReviewService(research).create(repo, study, actor="fixture", key="unconfirmed")
    assert error.value.code == ErrorCode.CONTROL_REQUESTED
    assert counts(history) == before


def test_changed_proposal_ticket_actor_or_config_rejects_until_new_review_then_reject_is_idempotent(
    history, tmp_path
):
    repo, study, research, service, identity, proposal, request = confirmed(history, tmp_path)
    before = counts(history)
    for changed in (
        evolve(request, proposal_ref="f" * 64),
        evolve(request, expected_config_version=request.expected_config_version + 1),
    ):
        with pytest.raises(AgentGitError) as error:
            service.decide(
                repo,
                identity,
                changed,
                actor="fixture",
                key="wrong-" + changed.proposal_ref[:2] + str(changed.expected_config_version),
            )
        assert error.value.code == ErrorCode.VERSION_CONFLICT
    for wrong_repo, actor in ((uuid4(), "fixture"), (repo, "someone-else")):
        with pytest.raises(AgentGitError) as error:
            service.decide(wrong_repo, identity, request, actor=actor, key="cross-scope")
        assert error.value.code == ErrorCode.NOT_FOUND
    assert counts(history) == before
    research.create_config(repo)
    advanced = counts(history)
    with pytest.raises(AgentGitError) as error:
        service.decide(repo, identity, request, actor="fixture", key="stale")
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    refreshed = service.begin(repo, identity, actor="fixture", key="fresh-review")
    assert refreshed["ticket"]["expected_config_version"] == request.expected_config_version + 1
    assert refreshed["ticket"]["proposal_ref"] == proposal["proposal_ref"]
    rejected = evolve(
        request,
        review_id=refreshed["review_id"],
        expected_config_version=refreshed["ticket"]["expected_config_version"],
        decision="reject",
    )
    saved = service.decide(repo, identity, rejected, actor="fixture", key="reject")
    assert service.decide(repo, identity, rejected, actor="fixture", key="reject") == saved
    with pytest.raises(AgentGitError):
        service.decide(
            repo, identity, evolve(rejected, decision="approve"), actor="fixture", key="reject"
        )
    assert counts(history) == {**advanced, "proposal_reviews": 1}
    assert service.view(repo, identity)["proposal_ref"] == proposal["proposal_ref"]


@pytest.mark.parametrize(
    "point", ["after_config_created", "before_decision_commit", "after_decision_commit"]
)
def test_review_config_and_decision_commit_atomically_and_retry_creates_one_version(
    history, tmp_path, point
):
    repo, _, research, service, identity, proposal, request = confirmed(history, tmp_path)
    before = counts(history)

    def fail(stage):
        if stage == point:
            raise RuntimeError("审批提交边界中断")

    service.fault = fail
    with pytest.raises(RuntimeError):
        service.decide(repo, identity, request, actor="fixture", key="approve")
    after = counts(history)
    committed = point == "after_decision_commit"
    assert after["agent_configs"] == before["agent_configs"] + committed
    assert after["proposal_reviews"] == committed
    assert after["jobs"] == before["jobs"] and after["runs"] == before["runs"]
    service.fault = lambda _: None
    saved = service.decide(repo, identity, request, actor="fixture", key="approve")
    assert service.decide(repo, identity, request, actor="fixture", key="approve") == saved
    assert counts(history) == {
        **before,
        "agent_configs": before["agent_configs"] + 1,
        "proposal_reviews": 1,
    }
    assert (
        research.config(repo, UUID(proposal["proposal"]["source_config_id"])).model_dump(
            mode="json"
        )
        == proposal["proposal"]["before"]
    )
    with pytest.raises(DBAPIError), history.engine.begin() as connection:
        connection.execute(TABLES["proposal_reviews"].update().values(actor="forged"))


def test_two_reviewers_compete_for_one_config_version_and_one_decision(history, tmp_path):
    repo, _, research, service, identity, _, request = confirmed(history, tmp_path)
    other = service.begin(repo, identity, actor="other-reviewer", key="review")
    requests = (
        ("fixture", request),
        ("other-reviewer", evolve(request, review_id=other["review_id"])),
    )
    before = counts(history)

    def approve(item):
        actor, body = item
        try:
            return RepairReviewService(research).decide(
                repo, identity, body, actor=actor, key="approve"
            )
        except AgentGitError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, requests))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count(ErrorCode.VERSION_CONFLICT) == 1
    assert counts(history) == {
        **before,
        "agent_configs": before["agent_configs"] + 1,
        "proposal_reviews": 1,
    }


def test_failed_confirmation_cannot_be_approved_or_saved_as_qualified_experiment(history, tmp_path):
    repo, _, _, service, identity, _, request = confirmed(history, tmp_path, "two_confirm_failures")
    before = counts(history)
    for decision in ("approve", "save_experiment"):
        with pytest.raises(AgentGitError) as error:
            service.decide(
                repo, identity, evolve(request, decision=decision), actor="fixture", key=decision
            )
        assert error.value.code == ErrorCode.FORBIDDEN
    assert counts(history) == before
    assert (
        service.decide(
            repo, identity, evolve(request, decision="reject"), actor="fixture", key="reject"
        )["decision"]["decision"]
        == "reject"
    )
