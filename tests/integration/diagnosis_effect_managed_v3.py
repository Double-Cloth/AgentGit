"""用已有 Job 完整快照固定语料，构造与原规格相同的正常/故障来源。"""

from types import SimpleNamespace
from uuid import UUID

from diagnosis_dataset import Faults, FixtureModel, resolve_selector

from agentgit.core.canonical import encode
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, ResearchGoal
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker


def execute_source(history, root, repo, config, case, dataset, *, enabled, branch_name):
    faults = Faults(case, enabled)
    research = ResearchService(history, root, corpus=faults.corpus())
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    branch = history.branch(repo, branch_name)
    queue = JobService(research)
    submitted = queue.enqueue(
        repo,
        task,
        actor="effect-source",
        key=branch_name,
        config_version=1,
        expected_branch_version=branch["version"],
        expected_head=branch["head_commit_id"],
        branch_name=branch_name,
    )

    class SourceWorker(Worker):
        def _attach(self, lease):
            session, origins, remaining = super()._attach(lease)
            faults.tools(session)
            return session, origins, remaining

    SourceWorker(queue, model_factory=lambda: FixtureModel(faults)).run_once()
    run_id = UUID(submitted.run_id)
    run = queue.run(repo, run_id)
    state = parse_contract(
        encode(history.restore(repo, run["checkpoint_commit_id"]).data), AgentState
    )
    return research, run_id, run, state, faults


def run_case(history, tmp_path, case, dataset):
    repo = history.create_repository("候选效果持久来源")
    initial = ResearchService(history, tmp_path / "configuration")
    config = initial.create_config(repo, AgentConfig(limits=dataset.limits))
    _, reference_id, reference, normal, clean = execute_source(
        history,
        tmp_path / "reference",
        repo,
        config,
        case,
        dataset,
        enabled=False,
        branch_name="main",
    )
    assert reference["status"] == "completed" and normal.evaluation.passed and not clean.receipts
    anchor = reference["checkpoint_commit_id"]
    reference_state = history.restore(repo, anchor)
    research, failed_id, failed, state, faults = execute_source(
        history,
        tmp_path / "failure",
        repo,
        config,
        case,
        dataset,
        enabled=True,
        branch_name="main",
    )
    assert failed["status"] == "failed" and state.error_code == case.expected_error.value
    if case.evaluation_error_prefix is not None:
        assert state.evaluation is not None and not state.evaluation.passed
        assert any(
            error.startswith(case.evaluation_error_prefix) for error in state.evaluation.errors
        )
    assert len(faults.receipts) == (0 if case.operation.startswith("corpus_") else 1)
    assert history.restore(repo, anchor) == reference_state
    environment = VersionService(history).checkout(repo, failed["checkpoint_commit_id"])[
        "environment"
    ]
    standin = SimpleNamespace(
        repository_id=repo,
        run_id=failed_id,
        environment=SimpleNamespace(input_ref=environment["input_ref"]),
    )
    return dict(
        case_id=case.id,
        split=case.split,
        test_only=True,
        annotation_origin=case.annotation.origin,
        annotation_review_status=case.annotation.review_status,
        reference_run_id=str(reference_id),
        failed_run_id=str(failed_id),
        repository_id=str(repo),
        common_ancestor=anchor,
        status=state.status.value,
        error_code=state.error_code,
        evaluation=state.evaluation.model_dump(mode="json") if state.evaluation else None,
        receipts=faults.receipts,
        resolved_labels=dict(
            injection=resolve_selector(history, standin, case.annotation.injection),
            first_visible=resolve_selector(history, standin, case.annotation.first_visible),
            repairable_decisions=[
                resolve_selector(history, standin, s) for s in case.annotation.repairable_decisions
            ],
        ),
        failed_usage=state.usage.model_dump(mode="json"),
        graph=BehaviorService(history).trace(repo, failed_id).model_dump(mode="json"),
        reference_graph=BehaviorService(history).trace(repo, reference_id).model_dump(mode="json"),
        corpus_changed=research.corpus.digest() != initial.corpus.digest(),
        source_kind="durable-jobs/full-frozen-corpus",
    )
