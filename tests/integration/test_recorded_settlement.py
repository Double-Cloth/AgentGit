"""历史先未知、后可信核对；纯重算不得提前使用回执或重复计量。"""

from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import ResearchGoal, evolve
from agentgit.core.versions import RecordedRange, RecordedReplay
from agentgit.replay.engine import RecordedReplayer
from agentgit.runtime.engine import ResearchRuntime
from agentgit.runtime.ports import VerifiedOutcome
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.reconciliation import ReconciliationService
from agentgit.services.recordings import RecordingReader
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def source(history, tmp_path, lost_step, control=None):
    research = ResearchService(history, tmp_path / "recorded-resolution")
    repository = history.create_repository("未知后可信模型核对")
    config = research.create_config(repository)
    task = research.create_task(repository, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    job = queue.enqueue(
        repository,
        task,
        actor="model-operator",
        key="model-resolution",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    actual, responses = [], {}

    class LostModel(MockResearchModel):
        def invoke(self, request):
            actual.append(request)
            responses[request] = super().invoke(request)
            if len(actual) == lost_step:
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "模型已完成但回执连接丢失")
            return responses[request]

    assert Worker(queue, model_factory=LostModel).run_once()["status"] == "outcome_unknown"
    stopped = queue.get(repository, UUID(job.job_id))["checkpoint_commit_id"]
    with history.engine.connect() as connection:
        call = dict(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == UUID(job.run_id),
                    TABLES["external_calls"].c.status == "outcome_unknown",
                )
            )
            .mappings()
            .one()
        )

    class Resolver:
        provider = "llm:mock-research/v1"
        signature = MockResearchModel.signature

        def resolve(self, request):
            return VerifiedOutcome(responses[request], b"verified-model-receipt")

    if control == "cancel":
        run = queue.run(repository, UUID(job.run_id))
        queue.request_control(
            repository, run["id"], "cancel", expected_version=run["state_version"]
        )
    resolution = ReconciliationService(history).confirm(
        repository,
        UUID(job.run_id),
        call["id"],
        actor="model-operator",
        expected_version=queue.run(repository, UUID(job.run_id))["state_version"],
        resolver=Resolver(),
    )
    if control == "deadline":
        with history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == UUID(job.job_id))
                .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    result = Worker(queue, model_factory=LostModel).run_once()
    assert (
        result["status"]
        == {None: "completed", "cancel": "cancelled", "deadline": "budget_exhausted"}[control]
    )
    final = queue.get(repository, UUID(job.job_id))["checkpoint_commit_id"]
    commits = [
        item["commit_id"] for item in reversed(VersionService(history).log(repository)["items"])
    ]
    environment = parse_contract(
        encode(VersionService(history).checkout(repository, final)["environment"]),
        EnvironmentManifest,
    )
    return queue, repository, job, commits, stopped, final, environment, actual, resolution


@pytest.mark.parametrize(
    "lost_step,control",
    [(1, None), (4, None), (11, None), (1, "cancel"), (11, "cancel"), (11, "deadline")],
)
def test_recorded_reconstructs_unknown_then_settles_verified_response_before_control(
    history, tmp_path, monkeypatch, lost_step, control
):
    queue, repo, job, commits, stopped, final, environment, actual, _ = source(
        history, tmp_path, lost_step, control
    )
    original = history.branch(repo)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=job.run_id, from_commit=commits[0], until_commit=final),
        expected_environment=environment,
    )
    assert len(bundle.settlements) == 1
    settled = []
    previous = ResearchRuntime._settle

    def settle(runtime, proof):
        assert runtime.state.status == RunStatus.OUTCOME_UNKNOWN
        settled.append(runtime.state.usage)
        return previous(runtime, proof)

    def forbidden(*args, **kwargs):
        pytest.fail("Recorded 核对来源不能调用真实模型、工具、Resolver、网络或工作区")

    manager = ManagementService(queue.research)
    submitted = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=job.run_id,
            from_commit=commits[0],
            until_commit=final,
            branch_name="recorded-resolution",
        ),
        "recorded-resolution",
    )
    with monkeypatch.context() as offline:
        offline.setattr(ResearchRuntime, "_settle", settle)
        for entry in (
            "agentgit.adapters.mock.MockResearchModel.invoke",
            "agentgit.adapters.tools.ToolRegistry.invoke",
            "agentgit.services.reconciliation.ReconciliationService.confirm",
            "socket.create_connection",
            "agentgit.adapters.workspace.ArtifactWorkspace.__init__",
            "agentgit.adapters.execution.engine.DockerEngine.__init__",
        ):
            offline.setattr(entry, forbidden)
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
    assert result["status"] == "completed", result
    assert len(settled) == 1 and settled[0].reserved_tokens > 0
    replay_job = manager.jobs.get(repo, UUID(submitted["job_id"]))
    assert history.restore(repo, replay_job["checkpoint_commit_id"]) == history.restore(repo, final)
    report = manager.replay_status(repo, UUID(submitted["job_id"]))["replay"]
    assert (
        report["verified"]
        and report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
    )
    assert report["logical_usage"]["model_calls"] == len(actual)
    assert history.branch(repo) == original
    with history.engine.connect() as connection:
        proofs = list(
            connection.execute(
                sa.select(TABLES["replay_checkpoints"]).where(
                    TABLES["replay_checkpoints"].c.job_id == UUID(submitted["job_id"])
                )
            ).mappings()
        )
    stopped_proof = next(row for row in proofs if row["source_commit_id"] == stopped)
    assert history.restore(repo, stopped_proof["commit_id"]).data["status"] == "outcome_unknown"


@pytest.mark.parametrize("target", ["evidence", "response", "diagnostic", "result_link"])
def test_recorded_resolution_missing_evidence_and_bad_ledger_cannot_fall_back(
    history, tmp_path, monkeypatch, target
):
    queue, repo, job, commits, _, final, _, actual, resolution = source(history, tmp_path, 4)
    manager = ManagementService(queue.research)
    submitted = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=job.run_id,
            from_commit=commits[0],
            until_commit=final,
            branch_name="missing-resolution",
        ),
        "missing-resolution",
    )
    removed = None
    if target == "result_link":
        with history.engine.begin() as connection:
            other = connection.execute(
                sa.select(TABLES["external_calls"].c.result_ref).where(
                    TABLES["external_calls"].c.run_id == UUID(job.run_id),
                    TABLES["external_calls"].c.id != resolution["call_id"],
                    TABLES["external_calls"].c.result_ref.is_not(None),
                )
            ).scalar()
            connection.execute(
                TABLES["external_calls"]
                .update()
                .where(TABLES["external_calls"].c.id == resolution["call_id"])
                .values(result_ref=other)
            )
    else:
        kind = ObjectType.BLOB
        reference = resolution["evidence_ref"]
        if target == "response":
            reference = history.objects.get_json(resolution["result_ref"], ObjectType.CALL)[
                "response_ref"
            ]
        elif target == "diagnostic":
            with history.engine.connect() as connection:
                reference = connection.execute(
                    sa.select(TABLES["object_refs"].c.object_id).where(
                        TABLES["object_refs"].c.repository_id == repo,
                        TABLES["object_refs"].c.owner_type == "external_call",
                        TABLES["object_refs"].c.owner_id == str(resolution["call_id"]),
                        TABLES["object_refs"].c.role == "uncertain_diagnostic",
                    )
                ).scalar_one()
            kind = ObjectType.CALL
        raw = history.objects.get(reference, kind)
        path = history.objects.path_for(reference)
        assert path.resolve().is_relative_to(history.objects.root.resolve())
        path.unlink()
        removed = reference, kind, raw

    def forbidden(*args, **kwargs):
        pytest.fail("核对录制缺失或错配不能查询模型服务或执行工具")

    try:
        monkeypatch.setattr("agentgit.adapters.mock.MockResearchModel.invoke", forbidden)
        monkeypatch.setattr("agentgit.adapters.tools.ToolRegistry.invoke", forbidden)
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
        assert result["status"] == "failed"
        assert result["error_code"] == (
            "replay_mismatch" if target == "result_link" else "recording_missing"
        )
        report = manager.replay_status(repo, UUID(submitted["job_id"]))["replay"]
        assert not report["verified"] and report["actual"]["model_calls"] == 0
        assert len(actual) == 11
    finally:
        if removed is not None:
            reference, kind, raw = removed
            history.objects.put(kind, raw)


def test_recorded_range_ending_unknown_does_not_use_a_later_confirmation(history, tmp_path):
    queue, repo, job, commits, stopped, _, environment, _, resolution = source(history, tmp_path, 4)
    reference = resolution["evidence_ref"]
    raw = history.objects.get(reference, ObjectType.BLOB)
    path = history.objects.path_for(reference)
    assert path.resolve().is_relative_to(history.objects.root.resolve())
    path.unlink()
    try:
        bundle = RecordingReader(history).load(
            repo,
            RecordedRange(source_run_id=job.run_id, from_commit=commits[0], until_commit=stopped),
            expected_environment=environment,
        )
        assert bundle.settlements == ()
        manager = ManagementService(queue.research)
        replay = manager.replay(
            repo,
            RecordedReplay(
                source_run_id=job.run_id,
                from_commit=commits[0],
                until_commit=stopped,
                branch_name="historical-unknown",
            ),
            "historical-unknown",
        )
        result = Worker(manager.jobs).run_once()
        assert result["status"] == "completed", result
        report = manager.replay_status(repo, UUID(replay["job_id"]))["replay"]
        assert report["verified"] and report["logical_status"] == "outcome_unknown"
        assert report["logical_usage"]["reserved_tokens"] > 0
    finally:
        history.objects.put(ObjectType.BLOB, raw)


@pytest.mark.parametrize("change", ["before_usage", "max_bytes", "unused"])
def test_recorded_settlement_rejects_budget_changes_and_unconsumed_proofs(
    history, tmp_path, change
):
    from dataclasses import replace

    queue, repo, job, commits, _, final, environment, _, _ = source(history, tmp_path, 4)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=job.run_id, from_commit=commits[0], until_commit=final),
        expected_environment=environment,
    )
    proof = bundle.settlements[0]
    if change == "before_usage":
        proof = replace(
            proof,
            before=evolve(
                proof.before, usage=evolve(proof.before.usage, tokens=proof.before.usage.tokens + 1)
            ),
        )
    elif change == "max_bytes":
        proof = replace(
            proof,
            record=replace(
                proof.record,
                intent=evolve(
                    proof.record.intent,
                    max_response_bytes=proof.record.intent.max_response_bytes + 1,
                ),
            ),
        )
    if change == "unused":
        bundle = replace(bundle, settlements=(proof, replace(proof, at_commit=commits[0])))
    else:
        bundle = replace(bundle, settlements=(proof,))
    config = queue.research.config(
        repo, UUID(queue.payload(queue.get(repo, UUID(job.job_id))).config_id)
    )
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_resolved_model_source_can_be_recomputed_through_compound_recorded(history, tmp_path):
    queue, repo, job, commits, _, final, _, _, _ = source(history, tmp_path, 4)
    manager = ManagementService(queue.research)
    first = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=job.run_id,
            from_commit=commits[0],
            until_commit=final,
            branch_name="resolved-first",
        ),
        "resolved-first",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    with history.engine.connect() as connection:
        proofs = list(
            connection.execute(
                sa.select(TABLES["replay_checkpoints"])
                .where(TABLES["replay_checkpoints"].c.job_id == UUID(first["job_id"]))
                .order_by(TABLES["replay_checkpoints"].c.source_order)
            ).mappings()
        )
    nested = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=first["run_id"],
            from_commit=proofs[0]["commit_id"],
            until_commit=proofs[-1]["commit_id"],
            branch_name="resolved-compound",
        ),
        "resolved-compound",
    )
    result = Worker(manager.jobs).run_once()
    assert result["status"] == "completed", result
    current = manager.jobs.get(repo, UUID(nested["job_id"]))
    assert history.restore(repo, current["checkpoint_commit_id"]) == history.restore(repo, final)
    report = manager.replay_status(repo, UUID(nested["job_id"]))["replay"]
    assert (
        report["verified"]
        and report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
    )
