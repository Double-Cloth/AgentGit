"""真实双分支批准、同事务双父/CAS、故障回滚与原键恢复。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from merge_proposal_cases import archive, history_fingerprint, move_head
from merge_proposal_cases import merge_case as merge_case
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from test_merge_proposals import expected, forbid_execution
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError
from agentgit.core.merge import MergedResearchState
from agentgit.core.merge_proposals import (
    ApproveMergeProposal,
    MergeEdits,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)
from agentgit.core.models import CommitManifest, parse_contract
from agentgit.core.versions import RecordedRange
from agentgit.services.history import TABLES
from agentgit.services.merge_proposals import MergeProposalService
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


def prepare(service, repo, request, actor="operator"):
    created = service.create(repo, request, actor=actor, key="create")
    identity = UUID(created["proposal_id"])
    validated = service.validate(
        repo, identity, ValidateMergeRevision(**expected(created)), actor=actor, key="validate"
    )
    reviewed = service.review(
        repo,
        identity,
        PrepareMergeReview(**expected(created), validation_id=validated["validation_id"]),
        actor=actor,
        key="review",
    )
    approval = ApproveMergeProposal(
        **expected(created),
        review_id=reviewed["review_id"],
        ticket_ref=reviewed["ticket_ref"],
        candidate_digest=created["candidate_digest"],
    )
    return identity, created, validated, reviewed, approval


def count(history, table):
    with history.engine.connect() as connection:
        return connection.execute(
            sa.select(sa.func.count()).select_from(TABLES[table])
        ).scalar_one()


def test_exact_approval_restores_dual_parent_history_without_execution(
    history, merge_case, tmp_path, monkeypatch, request
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    identity, created, validated, reviewed, approval = prepare(service, repo, pins)
    before = history_fingerprint(history, repo)
    result = service.approve(repo, identity, approval, actor="operator", key="approve")
    commit = result["commit_id"]
    branch = history.branch(repo, pins.ours.name)
    assert branch["head_commit_id"] == commit and branch["version"] == pins.ours.version + 1
    assert history.branch(repo, pins.theirs.name)["head_commit_id"] == pins.theirs.head
    restored = history.restore(repo, commit)
    state = parse_contract(encode(restored.data), MergedResearchState)
    assert state.execution == "requires_new_run" and "messages" not in restored.data
    original = service.show(repo, identity)["candidate"]
    assert state.model_dump(mode="json") == original["state"]
    for artifact in restored.artifacts:
        actual = history.objects.get(artifact.object_ref, ObjectType.BLOB)
        assert actual == next(
            item["text"].encode() for item in original["artifacts"] if item["path"] == artifact.path
        )
    versions = VersionService(history)
    log = versions.log(repo, branch_name=pins.ours.name, limit=1000)["items"]
    ids = [item["commit_id"] for item in log]
    assert ids[0] == commit and log[0]["parents"] == [pins.ours.head, pins.theirs.head]
    assert len(ids) == len(set(ids))
    position = {node: index for index, node in enumerate(ids)}
    for item in log:
        assert all(position[item["commit_id"]] < position[parent] for parent in item["parents"])
    assert history.log(repo, branch_name=pins.ours.name, limit=1000) == ids
    assert versions.checkout(repo, commit)["snapshot"] == restored.model_dump(mode="json")
    assert versions.diff(repo, pins.ours.head, commit)["right_commit"] == commit
    # 来源 Run 没有合并检查点，不能冒充其录制终点。
    with pytest.raises(AgentGitError):
        versions.select_range(
            repo,
            RecordedRange(
                source_run_id=pins.ours.source_run_id,
                from_commit=pins.ours.head,
                until_commit=commit,
            ),
        )
    after = history_fingerprint(history, repo)
    assert {key for key in before if before[key] != after[key]} == {
        "branches",
        "commits",
        "commit_parents",
        "artifacts",
    }
    reopened = MergeProposalService(history)
    assert reopened.show(repo, identity)["status"] == "merged"
    assert reopened.show(repo, identity)["approval"] == result
    assert not any(item["current"] for item in reopened.show(repo, identity)["reviews"])
    assert reopened.approve(repo, identity, approval, actor="operator", key="approve") == result
    assert count(history, "merge_approvals") == 1
    with (
        pytest.raises(DBAPIError, match="merge parents already published"),
        history.engine.begin() as connection,
    ):
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repo,
                commit_id=commit,
                parent_order=1,
                parent_id=pins.ours.head,
            )
        )
    for action in (
        lambda: reopened.approve(repo, identity, approval, actor="operator", key="again"),
        lambda: reopened.reject(
            repo,
            identity,
            RejectMergeProposal(**expected(created), reason="已合并不能拒绝"),
            actor="operator",
            key="reject",
        ),
        lambda: reopened.replace(
            repo,
            identity,
            ReplaceMergeRevision(**expected(created), edits=MergeEdits()),
            actor="operator",
            key="edit",
        ),
    ):
        with pytest.raises(AgentGitError):
            action()
    assert history_fingerprint(history, repo) == after
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/final",
        dict(
            result=result,
            approval=approval.model_dump(mode="json"),
            reviewed=reviewed,
            validated=validated,
        ),
    )


@pytest.mark.parametrize(
    "point",
    ["merge.commit_stored", "merge.approval_stored", "merge.head_updated", "merge.after_commit"],
)
def test_approval_crash_has_atomic_history_and_original_key_recovers(
    history, merge_case, tmp_path, monkeypatch, request, point
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    identity, _, _, _, approval = prepare(service, repo, pins)
    before = history_fingerprint(history, repo)

    def fail(stage):
        if stage == point:
            raise RuntimeError("批准事务注入中断")

    history.fault = fail
    with pytest.raises(RuntimeError, match="注入中断"):
        service.approve(repo, identity, approval, actor="operator", key="approve")
    history.fault = lambda _: None
    if point != "merge.after_commit":
        assert history_fingerprint(history, repo) == before
        assert count(history, "merge_approvals") == 0
    else:
        assert count(history, "merge_approvals") == 1
        assert service.show(repo, identity)["status"] == "merged"
    result = MergeProposalService(history).approve(
        repo, identity, approval, actor="operator", key="approve"
    )
    assert history.branch(repo, pins.ours.name)["head_commit_id"] == result["commit_id"]
    assert count(history, "merge_approvals") == 1
    assert count(history, "commits") == len(
        VersionService(history).ancestors(repo, result["commit_id"])
    )
    archive(history, repo, tmp_path, request.node.name + "/final", dict(result=result, point=point))


def test_competing_actors_can_publish_only_one_approval(
    history, merge_case, tmp_path, monkeypatch, request
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    identity, created, validated, _, approval = prepare(service, repo, pins, actor="one")
    ticket = service.review(
        repo,
        identity,
        PrepareMergeReview(**expected(created), validation_id=validated["validation_id"]),
        actor="two",
        key="review",
    )
    second = ApproveMergeProposal(
        **expected(created),
        review_id=ticket["review_id"],
        ticket_ref=ticket["ticket_ref"],
        candidate_digest=created["candidate_digest"],
    )

    def submit(pair):
        actor, payload = pair
        try:
            return service.approve(repo, identity, payload, actor=actor, key="approve")
        except AgentGitError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, (("one", approval), ("two", second))))
    assert sum(result is not None for result in results) == 1
    assert count(history, "merge_approvals") == 1
    assert history.branch(repo, pins.ours.name)["version"] == pins.ours.version + 1
    archive(history, repo, tmp_path, request.node.name + "/final", dict(results=results))


def test_mismatched_approval_and_unauthorized_dual_parents_leave_history_unchanged(
    history, merge_case, tmp_path, monkeypatch, request
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    identity, created, _, _, approval = prepare(service, repo, pins)
    before = history_fingerprint(history, repo)
    for index, override in enumerate(
        (
            dict(ticket_ref="0" * 64),
            dict(candidate_digest="0" * 64),
            dict(expected_revision_ref="0" * 64),
            dict(review_id=str(uuid4())),
        )
    ):
        payload = parse_contract(
            encode({**approval.model_dump(mode="json"), **override}), ApproveMergeProposal
        )
        with pytest.raises(AgentGitError):
            service.approve(repo, identity, payload, actor="operator", key=f"wrong-{index}")
    with pytest.raises(AgentGitError):
        service.approve(repo, identity, approval, actor="other", key="wrong-actor")
    manifest = parse_contract(
        history.objects.get(pins.ours.head, ObjectType.COMMIT), CommitManifest
    )
    unauthorized = parse_contract(
        encode({**manifest.model_dump(mode="json"), "parents": [pins.ours.head, pins.theirs.head]}),
        CommitManifest,
    )
    obj = history.objects.put_json(ObjectType.COMMIT, unauthorized.model_dump(mode="json"))
    with (
        pytest.raises(DBAPIError, match="dual parents require exact approval"),
        history.engine.begin() as connection,
    ):
        history._ref(connection, repo, obj, "commit", obj.id, "manifest")
        connection.execute(
            TABLES["commits"]
            .insert()
            .values(
                id=obj.id,
                repository_id=repo,
                state_ref=manifest.state_ref,
                environment_ref=manifest.environment_ref,
                event_manifest_ref=manifest.event_manifest_ref,
            )
        )
        for index, parent in enumerate(unauthorized.parents):
            connection.execute(
                TABLES["commit_parents"]
                .insert()
                .values(repository_id=repo, commit_id=obj.id, parent_order=index, parent_id=parent)
            )
    assert history_fingerprint(history, repo) == before
    service.replace(
        repo,
        identity,
        ReplaceMergeRevision(**expected(created), edits=MergeEdits()),
        actor="operator",
        key="edit",
    )
    with pytest.raises(AgentGitError):
        service.approve(repo, identity, approval, actor="operator", key="old-ticket")
    assert history_fingerprint(history, repo) == before and count(history, "merge_approvals") == 0
    archive(history, repo, tmp_path, request.node.name + "/final", dict(result="all_rejected"))


def test_changed_source_head_invalidates_approval(
    history, merge_case, tmp_path, monkeypatch, request
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    service = manager.merge_proposals
    identity, _, _, _, approval = prepare(service, repo, pins)
    move_head(history, repo, pins.theirs.name)
    before = history_fingerprint(history, repo)
    with pytest.raises(AgentGitError):
        service.approve(repo, identity, approval, actor="operator", key="stale")
    assert history_fingerprint(history, repo) == before and count(history, "merge_approvals") == 0
    assert service.show(repo, identity)["status"] == "stale"
    archive(history, repo, tmp_path, request.node.name + "/final", dict(result="stale"))


def test_http_approval_and_cli_original_key_recovery(
    history, merge_case, tmp_path, monkeypatch, request
):
    repo, _, manager, pins, _ = merge_case
    forbid_execution(monkeypatch)
    identity, _, _, _, approval = prepare(manager.merge_proposals, repo, pins, actor=manager.actor)
    token = "merge-approval-local-test-token-12345"

    @contextmanager
    def management(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", management)
    with TestClient(
        create_app(Settings(api_token=SecretStr(token)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + token, "X-Repository-ID": str(repo)},
    ) as client:
        url = f"/merge-proposals/{identity}/approve"
        response = client.post(
            url, json=approval.model_dump(mode="json"), headers={"Idempotency-Key": "approve"}
        )
        assert response.status_code == 201, response.text
        result = response.json()
        path = tmp_path / "approve.json"
        path.write_bytes(encode(approval.model_dump(mode="json")))
        output = CliRunner().invoke(
            app,
            ["merge-approve", str(repo), str(identity), "--file", str(path), "--key", "approve"],
        )
        assert output.exit_code == 0, output.output
        assert json.loads(output.stdout) == result
        assert (
            client.post(
                url, json=approval.model_dump(mode="json"), headers={"Idempotency-Key": "approve"}
            ).json()
            == result
        )

        def no_cas(*args, **kwargs):
            pytest.fail("重开查询不能写 CAS")

        with monkeypatch.context() as read:
            read.setattr(history.objects, "put", no_cas)
            assert client.get(f"/merge-proposals/{identity}").json()["approval"] == result
        assert count(history, "merge_approvals") == 1
    archive(history, repo, tmp_path, request.node.name + "/final", dict(result=result))
