"""真实 PostgreSQL 中提案、修订、审阅准备与拒绝的原子边界。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from merge_proposal_cases import archive, history_fingerprint, move_head
from merge_proposal_cases import merge_case as merge_case
from sqlalchemy.exc import DBAPIError

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge_proposals import (
    MergeEdits,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.versions import ArtifactPatch
from agentgit.services.history import TABLES
from agentgit.services.merge_proposals import MergeProposalService

pytestmark = pytest.mark.integration


def expected(receipt):
    return dict(
        expected_revision=receipt["revision"], expected_revision_ref=receipt["revision_ref"]
    )


def forbid_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("合并提案操作不能调用模型/工具或提交执行任务")

    monkeypatch.setattr("agentgit.adapters.mock.MockResearchModel.invoke", forbidden)
    monkeypatch.setattr("agentgit.adapters.tools.ToolRegistry.invoke", forbidden)
    monkeypatch.setattr("agentgit.services.jobs.JobService.enqueue", forbidden)


def test_revision_invalidates_old_validation_and_survives_service_reopen(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    before = history_fingerprint(history, repo)
    created = service.create(repo, request, actor="operator", key="create")
    identity = UUID(created["proposal_id"])
    assert service.show(repo, identity)["status"] == "draft"
    checked = service.validate(
        repo,
        identity,
        ValidateMergeRevision(**expected(created)),
        actor="operator",
        key="validate-1",
    )
    assert checked["record"]["result"]["validated"]
    review = PrepareMergeReview(**expected(created), validation_id=checked["validation_id"])
    ticket = service.review(repo, identity, review, actor="operator", key="review-1")
    changed = service.replace(
        repo,
        identity,
        ReplaceMergeRevision(**expected(created), edits=MergeEdits()),
        actor="operator",
        key="edit",
    )
    assert changed["revision"] == 2 and changed["revision_ref"] != created["revision_ref"]
    assert changed["candidate_digest"] == created["candidate_digest"]
    assert service.show(repo, identity)["validation"] is None
    with pytest.raises(AgentGitError) as error:
        service.review(repo, identity, review, actor="operator", key="stale-review")
    assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert service.review(repo, identity, review, actor="operator", key="review-1") == ticket
    assert service.create(repo, request, actor="operator", key="create") == created
    reopened = MergeProposalService(history)
    assert reopened.show(repo, identity)["revision"] == changed
    validation = reopened.validate(
        repo,
        identity,
        ValidateMergeRevision(**expected(changed)),
        actor="operator",
        key="validate-2",
    )
    new_ticket = reopened.review(
        repo,
        identity,
        PrepareMergeReview(**expected(changed), validation_id=validation["validation_id"]),
        actor="operator",
        key="review-2",
    )
    assert new_ticket["ticket"]["revision_ref"] == changed["revision_ref"] and new_ticket != ticket
    view = reopened.show(repo, identity)
    tickets = {item["review_id"]: item for item in view["reviews"]}
    assert (
        tickets[new_ticket["review_id"]]["current"] and not tickets[ticket["review_id"]]["current"]
    )
    assert (
        reopened.review_status(repo, identity, UUID(ticket["review_id"]))
        == tickets[ticket["review_id"]]
    )
    page = reopened.show(repo, identity, review_before=UUID(new_ticket["review_id"]))
    assert [item["review_id"] for item in page["reviews"]] == [ticket["review_id"]]
    reject = RejectMergeProposal(**expected(changed), reason="保留人工审阅意见")
    decision = reopened.reject(repo, identity, reject, actor="operator", key="reject")
    assert reopened.reject(repo, identity, reject, actor="operator", key="reject") == decision
    assert reopened.show(repo, identity)["status"] == "rejected"
    assert all(not item["current"] for item in reopened.show(repo, identity)["reviews"])
    with pytest.raises(AgentGitError):
        reopened.replace(
            repo,
            identity,
            ReplaceMergeRevision(**expected(changed), edits=MergeEdits()),
            actor="operator",
            key="after-close",
        )
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "revision-reopen/final",
        dict(
            created=created,
            changed=changed,
            validation=validation,
            old_ticket=ticket,
            new_ticket=new_ticket,
            decision=decision,
            view=reopened.show(repo, identity),
        ),
    )


@pytest.mark.parametrize("merge_case", [True], indirect=True, ids=["bad-quote"])
def test_failed_artifact_can_be_edited_and_requires_a_new_exact_validation(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, session = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    before = history_fingerprint(history, repo)
    receipt = service.create(repo, request, actor="operator", key="create")
    identity = UUID(receipt["proposal_id"])
    failed = service.validate(
        repo,
        identity,
        ValidateMergeRevision(**expected(receipt)),
        actor="operator",
        key="bad-validation",
    )
    assert not failed["record"]["result"]["validated"]
    assert service.show(repo, identity)["status"] == "needs_review"
    with pytest.raises(AgentGitError):
        service.review(
            repo,
            identity,
            PrepareMergeReview(**expected(receipt), validation_id=failed["validation_id"]),
            actor="operator",
            key="bad-review",
        )
    correct = history.restore(repo, session.head)
    edits = MergeEdits(
        artifacts=tuple(
            ArtifactPatch(
                path=item.path,
                text=history.objects.get(item.object_ref, ObjectType.BLOB).decode(),
                media_type=item.media_type,
            )
            for item in correct.artifacts
        )
    )
    changed = service.replace(
        repo,
        identity,
        ReplaceMergeRevision(**expected(receipt), edits=edits),
        actor="operator",
        key="fix",
    )
    assert service.show(repo, identity)["status"] == "draft"
    valid = service.validate(
        repo,
        identity,
        ValidateMergeRevision(**expected(changed)),
        actor="operator",
        key="fixed-validation",
    )
    assert valid["record"]["result"]["validated"]
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "artifact-edit/final",
        dict(
            initial=receipt,
            failed=failed,
            changed=changed,
            validated=valid,
            view=service.show(repo, identity),
        ),
    )


def test_same_key_retries_and_competing_edits_or_rejections_publish_once(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    before = history_fingerprint(history, repo)

    def create(_):
        return service.create(repo, request, actor="operator", key="same-create")

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(create, range(2)))
    assert receipts[0] == receipts[1]
    created = receipts[0]
    identity = UUID(created["proposal_id"])
    edit = ReplaceMergeRevision(**expected(created), edits=MergeEdits())

    def replace(actor):
        try:
            return service.replace(repo, identity, edit, actor=actor, key="edit")
        except AgentGitError as error:
            return error.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(replace, ("one", "two")))
    assert (
        sum(isinstance(item, dict) for item in outcomes) == 1
        and ErrorCode.VERSION_CONFLICT.value in outcomes
    )
    current = service.show(repo, identity)["revision"]

    def reject(actor):
        try:
            return service.reject(
                repo,
                identity,
                RejectMergeProposal(**expected(current), reason="竞争审阅拒绝"),
                actor=actor,
                key="reject",
            )
        except AgentGitError as error:
            return error.code.value

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(reject, ("one", "two")))
    assert sum(isinstance(item, dict) for item in decisions) == 1
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["merge_proposals"])
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["merge_revisions"])
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["merge_rejections"])
            ).scalar_one()
            == 1
        )
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "concurrency/final",
        dict(
            create=created, edits=outcomes, rejections=decisions, view=service.show(repo, identity)
        ),
    )


def test_changed_head_blocks_validation_and_review_but_can_be_rejected(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    created = service.create(repo, request, actor="operator", key="create")
    identity = UUID(created["proposal_id"])
    checked = service.validate(
        repo, identity, ValidateMergeRevision(**expected(created)), actor="operator", key="check"
    )
    move_head(history, repo, "ours")
    before = history_fingerprint(history, repo)
    for action in (
        lambda: service.validate(
            repo,
            identity,
            ValidateMergeRevision(**expected(created)),
            actor="operator",
            key="stale-check",
        ),
        lambda: service.review(
            repo,
            identity,
            PrepareMergeReview(**expected(created), validation_id=checked["validation_id"]),
            actor="operator",
            key="stale-review",
        ),
    ):
        with pytest.raises(AgentGitError) as error:
            action()
        assert error.value.code == ErrorCode.VERSION_CONFLICT
    assert service.show(repo, identity)["status"] == "stale"
    decision = service.reject(
        repo,
        identity,
        RejectMergeProposal(**expected(created), reason="来源分支已推进"),
        actor="operator",
        key="reject-stale",
    )
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "head-changed/final",
        dict(
            created=created,
            validation=checked,
            decision=decision,
            view=service.show(repo, identity),
        ),
    )


def test_transaction_interruption_rolls_back_owned_rows_and_retry_keeps_one_proposal(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    before = history_fingerprint(history, repo)

    def fail(stage):
        if stage == "merge.revision_stored":
            raise RuntimeError("injected-before-commit")

    with monkeypatch.context() as breaking:
        breaking.setattr(history, "fault", fail)
        with pytest.raises(RuntimeError, match="injected-before-commit"):
            service.create(repo, request, actor="operator", key="retry")
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["merge_proposals"])
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["merge_revisions"])
            ).scalar_one()
            == 0
        )
    created = service.create(repo, request, actor="operator", key="retry")
    identity = UUID(created["proposal_id"])
    assert service.create(repo, request, actor="operator", key="retry") == created
    for name in ("merge_proposals", "merge_revisions"):
        with pytest.raises(DBAPIError), history.engine.begin() as connection:
            connection.execute(TABLES[name].delete())
    with pytest.raises(AgentGitError) as error:
        service.show(uuid4(), identity)
    assert error.value.code == ErrorCode.NOT_FOUND
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "rollback/final",
        dict(created=created, view=service.show(repo, identity)),
    )
