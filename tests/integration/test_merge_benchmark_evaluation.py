"""三方法候选比较与主方法批准后执行；原型和正式运行分目录。"""

import os
from uuid import UUID

import pytest
import sqlalchemy as sa
from merge_benchmark_cases import build_case, specs
from merge_benchmark_methods import METHODS, assess, clean_expectation, inputs, run_method
from merge_proposal_cases import archive, history_fingerprint, rows_for
from test_merge_approval import prepare

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge_proposals import (
    PrepareMergeReview,
    RejectMergeProposal,
    ValidateMergeRevision,
)
from agentgit.core.merge_runs import ForkMergedBranch, NewMergedRun
from agentgit.core.models import CommitManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetLimits, BudgetUsage
from agentgit.core.snapshots import StateSnapshot
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def counters(rows):
    return {name: len(rows[name]) for name in ("runs", "jobs", "external_calls")}


def no_execution(*_args, **_kwargs):
    raise AssertionError("候选方法不得执行或发布对象")


def publish_and_run(history, repo, research, manager, payload, method):
    service = manager.merge_proposals
    identity, created, validation, review, request = prepare(
        service, repo, payload, actor=manager.actor
    )
    assert created["candidate_digest"] == method["validation"]["candidate_digest"]
    before_calls = counters(rows_for(history, repo))
    approval = manager.approve_merge_proposal(repo, identity, request, "approve")
    assert counters(rows_for(history, repo)) == before_calls
    commit = parse_contract(
        history.objects.get(approval["commit_id"], ObjectType.COMMIT), CommitManifest
    )
    assert commit.parents == (payload.ours.head, payload.theirs.head)
    assert history.branch(repo, payload.ours.name)["head_commit_id"] == approval["commit_id"]
    assert history.branch(repo, payload.theirs.name)["head_commit_id"] == payload.theirs.head
    snapshot = history.restore(repo, approval["commit_id"])
    assert snapshot.data == method["state"]
    actual = {
        entry.path: history.objects.get(entry.object_ref, ObjectType.BLOB).decode()
        for entry in snapshot.artifacts
    }
    assert actual == {item["path"]: item["text"] for item in method["artifacts"]}
    constraints = method["state"]["constraints"]
    limits = parse_contract(encode(constraints["limits"]), BudgetLimits)
    config_id = research.create_config(
        repo,
        AgentConfig(
            prompt="按已批准合并计划执行固定研究任务。",
            tools=tuple(constraints["tools"]),
            limits=limits,
        ),
    )
    with history.engine.connect() as connection:
        version = connection.execute(
            sa.select(TABLES["agent_configs"].c.version).where(
                TABLES["agent_configs"].c.id == config_id
            )
        ).scalar_one()
    manager.fork_merged_branch(
        repo,
        ForkMergedBranch(
            approval_id=approval["decision_id"],
            commit_id=approval["commit_id"],
            name="merged-evaluation",
        ),
        "fork-merged-evaluation",
    )
    submitted = manager.start_merged_run(
        repo,
        NewMergedRun(
            branch_name="merged-evaluation",
            expected_head=approval["commit_id"],
            expected_branch_version=0,
            config_id=str(config_id),
            config_version=version,
            validator_ref=payload.validator_ref,
            budget=limits,
        ),
        "start-merged-evaluation",
    )
    job = manager.jobs.get(repo, UUID(submitted["job_id"]))
    spec = manager.jobs.payload(job).merged
    assert spec is not None
    initial = parse_contract(
        history.objects.get(spec.initial_state_ref, ObjectType.STATE), StateSnapshot
    )
    agent = parse_contract(encode(initial.data), AgentState)
    assert agent.usage == BudgetUsage() and agent.completed == agent.messages == ()
    assert agent.pending_tool is None and agent.evaluation is None
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    job = manager.jobs.get(repo, UUID(submitted["job_id"]))
    run = manager.jobs.run(repo, job["run_id"])
    final = parse_contract(
        encode(history.restore(repo, run["checkpoint_commit_id"]).data), AgentState
    )
    assert final.evaluation is not None and final.evaluation.passed
    assert {node.key for node in final.plan.nodes} == {
        node["key"] for node in method["state"]["plan"]["nodes"]
    }
    assert set(final.completed) == {node.key for node in final.plan.nodes}
    return dict(
        created=created,
        validation=validation,
        review=review,
        approval=approval,
        submitted=submitted,
        final_run_id=str(job["run_id"]),
        final_commit=run["checkpoint_commit_id"],
        evaluation=final.evaluation.model_dump(),
        automated_review_tickets=1,
        independent_human_reviews=None,
    )


def keep_blocked(history, repo, manager, payload, method):
    before = history_fingerprint(history, repo)
    try:
        created = manager.create_merge_proposal(repo, payload, "create-blocked")
    except AgentGitError as error:
        assert error.code in (ErrorCode.VALIDATION, ErrorCode.FORBIDDEN, ErrorCode.VERSION_CONFLICT)
        assert history_fingerprint(history, repo) == before
        return dict(
            publication_guard=error.as_dict(),
            automated_review_tickets=0,
            independent_human_reviews=None,
        )
    identity = UUID(created["proposal_id"])
    expected = dict(
        expected_revision=created["revision"], expected_revision_ref=created["revision_ref"]
    )
    validation = manager.validate_merge_revision(
        repo, identity, ValidateMergeRevision(**expected), "validate-blocked"
    )
    assert not validation["record"]["result"]["validated"]
    assert created["candidate_digest"] == method["validation"]["candidate_digest"]
    with pytest.raises(AgentGitError):
        manager.begin_merge_review(
            repo,
            identity,
            PrepareMergeReview(**expected, validation_id=validation["validation_id"]),
            "invalid-review",
        )
    rejection = manager.reject_merge_proposal(
        repo,
        identity,
        RejectMergeProposal(**expected, reason="冻结样例存在阻断项，自动测试关闭提案。"),
        "reject-blocked",
    )
    assert history_fingerprint(history, repo) == before
    return dict(
        created=created,
        validation=validation,
        rejection=rejection,
        automated_review_tickets=0,
        independent_human_reviews=None,
    )


@pytest.mark.parametrize("spec", specs(), ids=lambda item: item["id"])
def test_merge_benchmark_methods_and_approved_execution(history, tmp_path, spec, monkeypatch):
    stage = os.environ.get("AGENTGIT_MERGE_BENCHMARK_STAGE", "pilot")
    assert stage in ("pilot", "formal")
    if stage == "formal":
        from merge_benchmark_release import verify_release

        verify_release()
    repo, research, manager, payload, construction = build_case(history, tmp_path, spec)
    original_rows = rows_for(history, repo)
    source_inputs = inputs(history, repo, payload, original_rows)
    expected = clean_expectation(source_inputs) if spec["expected"] == "accept" else None
    archive(history, repo, tmp_path, spec["id"] + "/source", construction)
    methods = []
    for name in METHODS:
        with monkeypatch.context() as readonly:
            readonly.setattr(history.objects, "put", no_execution)
            readonly.setattr("agentgit.adapters.mock.MockResearchModel.invoke", no_execution)
            readonly.setattr("agentgit.adapters.tools.ToolRegistry.invoke", no_execution)
            method = run_method(name, history, repo, payload, source_inputs)
        assert rows_for(history, repo) == original_rows
        method["assessment"] = assess(spec, method, expected)
        method["actual_new_calls"] = 0
        methods.append(method)
    archive(
        history,
        repo,
        tmp_path,
        spec["id"] + "/methods",
        dict(
            construction=construction,
            methods=methods,
            stage=stage,
            result="candidate-methods-read-only",
            proposal=payload.model_dump(mode="json"),
        ),
    )
    semantic = methods[0]
    assert semantic["assessment"]["matches_expectation"]
    if spec["expected"] == "accept":
        workflow = publish_and_run(history, repo, research, manager, payload, semantic)
    else:
        workflow = keep_blocked(history, repo, manager, payload, semantic)
    archive(
        history,
        repo,
        tmp_path,
        spec["id"] + "/final",
        dict(
            construction=construction,
            methods=methods,
            workflow=workflow,
            stage=stage,
            proposal=payload.model_dump(mode="json"),
            result="merge-benchmark-" + stage,
        ),
    )
