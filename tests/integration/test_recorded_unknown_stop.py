"""旧 Run 调用未观察即崩溃，接管封存和可信核对必须分别重算。"""

from dataclasses import replace
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


class LostProcess(BaseException):
    pass


def recovered_source(history, tmp_path, lost_step=1, crash="result", control=None, confirmed=True):
    research = ResearchService(history, tmp_path / "recovered-unknown")
    repo = history.create_repository("未知调用接管封存重算")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    submitted = queue.enqueue(
        repo,
        task,
        actor="recovery-operator",
        key="recovery-unknown",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    job, original = UUID(submitted.job_id), UUID(submitted.run_id)
    actual, responses = [], {}

    class LostModel(MockResearchModel):
        def invoke(self, request):
            actual.append(request)
            responses[request] = super().invoke(request)
            if len(actual) == lost_step:
                if crash == "invoke":
                    raise LostProcess()
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "模型服务已有结果，回执丢失")
            return responses[request]

    def fault(point):
        if point == "after_result":
            with history.engine.connect() as connection:
                unknown = connection.execute(
                    sa.select(TABLES["external_calls"].c.id).where(
                        TABLES["external_calls"].c.run_id == original,
                        TABLES["external_calls"].c.status == "outcome_unknown",
                    )
                ).first()
            if unknown is not None:
                raise LostProcess()

    with pytest.raises(LostProcess):
        Worker(queue, model_factory=LostModel, fault=fault).run_once()
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == job)
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    sealed = Worker(queue, model_factory=LostModel).run_once()
    assert sealed["status"] == "outcome_unknown", sealed
    source = UUID(sealed["run_id"])
    assert source != original and queue.run(repo, original)["status"] == "interrupted"
    assert len(actual) == lost_step
    stop_commit = queue.get(repo, job)["checkpoint_commit_id"]
    base = queue.run(repo, source)["base_commit_id"]
    with history.engine.connect() as connection:
        call = dict(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == original,
                    TABLES["external_calls"].c.status.in_(("started", "outcome_unknown")),
                )
            )
            .mappings()
            .one()
        )

    if not confirmed:
        environment = parse_contract(
            encode(VersionService(history).checkout(repo, stop_commit)["environment"]),
            EnvironmentManifest,
        )
        return queue, repo, source, base, stop_commit, stop_commit, environment, actual, call, None

    class Resolver:
        provider = "llm:mock-research/v1"
        signature = MockResearchModel.signature

        def resolve(self, request):
            return VerifiedOutcome(responses[request], b"trusted-recovered-model-receipt")

    if control == "cancel":
        run = queue.run(repo, source)
        queue.request_control(repo, source, "cancel", expected_version=run["state_version"])
    resolution = ReconciliationService(history).confirm(
        repo,
        source,
        call["id"],
        actor="recovery-operator",
        expected_version=queue.run(repo, source)["state_version"],
        resolver=Resolver(),
    )
    if control == "deadline":
        with history.engine.begin() as connection:
            connection.execute(
                TABLES["jobs"]
                .update()
                .where(TABLES["jobs"].c.id == job)
                .values(deadline_at=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
            )
    final = Worker(queue, model_factory=LostModel).run_once()
    assert (
        final["status"]
        == {None: "completed", "cancel": "cancelled", "deadline": "budget_exhausted"}[control]
    ), final
    head = queue.get(repo, job)["checkpoint_commit_id"]
    environment = parse_contract(
        encode(VersionService(history).checkout(repo, head)["environment"]), EnvironmentManifest
    )
    return queue, repo, source, base, stop_commit, head, environment, actual, call, resolution


@pytest.mark.parametrize(
    "lost_step,crash,control",
    [
        (1, "result", None),
        (4, "result", None),
        (11, "result", "cancel"),
        (11, "result", "deadline"),
        (1, "invoke", None),
    ],
)
def test_recorded_recomputes_recovery_unknown_and_settlement_without_a_new_call(
    history, tmp_path, monkeypatch, lost_step, crash, control
):
    queue, repo, source, base, stop, head, environment, actual, _, _ = recovered_source(
        history, tmp_path, lost_step, crash, control
    )
    original = history.branch(repo)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=str(source), from_commit=base, until_commit=head),
        expected_environment=environment,
    )
    assert len(bundle.unknown_stops) == len(bundle.settlements) == 1
    assert bundle.unknown_stops[0].source_commit == stop
    assert (
        history.restore(repo, stop).data["messages"] == history.restore(repo, base).data["messages"]
    )
    manager = ManagementService(queue.research)
    submitted = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=str(source),
            from_commit=base,
            until_commit=head,
            branch_name="recorded-recovered",
        ),
        "recorded-recovered",
    )

    def forbidden(*args, **kwargs):
        pytest.fail("接管封存 Recorded 不得初始化或调用模型、Resolver、工具或真实执行")

    with monkeypatch.context() as offline:
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
    replay_job = manager.jobs.get(repo, UUID(submitted["job_id"]))
    assert history.restore(repo, replay_job["checkpoint_commit_id"]) == history.restore(repo, head)
    report = manager.replay_status(repo, UUID(submitted["job_id"]))["replay"]
    assert (
        report["verified"]
        and report["actual"]["model_calls"] == report["actual"]["tool_calls"] == 0
    )
    assert report["logical_usage"]["model_calls"] == len(actual)
    assert report["logical_usage"]["reserved_tokens"] == 0
    assert history.branch(repo) == original

    if lost_step == 4:
        with history.engine.connect() as connection:
            points = list(
                connection.execute(
                    sa.select(TABLES["replay_checkpoints"])
                    .where(TABLES["replay_checkpoints"].c.job_id == UUID(submitted["job_id"]))
                    .order_by(TABLES["replay_checkpoints"].c.source_order)
                ).mappings()
            )
        nested = manager.replay(
            repo,
            RecordedReplay(
                source_run_id=submitted["run_id"],
                from_commit=points[0]["commit_id"],
                until_commit=points[-1]["commit_id"],
                branch_name="sealed-compound",
            ),
            "sealed-compound",
        )
        result = Worker(manager.jobs, model_factory=forbidden).run_once()
        assert result["status"] == "completed", result
        report = manager.replay_status(repo, UUID(nested["job_id"]))["replay"]
        assert report["verified"] and report["actual"]["model_calls"] == 0


@pytest.mark.parametrize("crash", ["invoke", "result"])
def test_unconfirmed_recovery_stop_is_verified_as_unknown_without_response(
    history, tmp_path, crash
):
    queue, repo, source, base, stop, _, _, actual, _, _ = recovered_source(
        history, tmp_path, crash=crash, confirmed=False
    )
    manager = ManagementService(queue.research)
    submitted = manager.replay(
        repo,
        RecordedReplay(
            source_run_id=str(source),
            from_commit=base,
            until_commit=stop,
            branch_name="unknown-only",
        ),
        "unknown-only",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    report = manager.replay_status(repo, UUID(submitted["job_id"]))["replay"]
    assert report["verified"] and report["logical_status"] == "outcome_unknown"
    assert report["logical_usage"]["model_calls"] == len(actual) == 1
    assert report["logical_usage"]["reserved_tokens"] > 0


def test_unknown_only_range_does_not_read_later_confirmed_response(history, tmp_path):
    queue, repo, source, base, stop, _, environment, _, _, resolution = recovered_source(
        history, tmp_path
    )
    result = history.objects.get_json(resolution["result_ref"], ObjectType.CALL)
    # 删除范围以外的回执和证据；只重算未知封存，不提前消费已核对结果。
    for digest in (result["response_ref"], resolution["evidence_ref"]):
        path = history.objects.path_for(digest)
        assert path.is_relative_to(history.objects.root)
        path.unlink()
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=str(source), from_commit=base, until_commit=stop),
        expected_environment=environment,
    )
    config = queue.research.config(repo, queue.run(repo, source)["agent_config_id"])
    replayed = RecordedReplayer(history.objects.get).run(bundle, config, queue.research.corpus)
    assert replayed.state.status == RunStatus.OUTCOME_UNKNOWN
    assert replayed.state.usage.reserved_tokens > 0 and replayed.consumed_calls == 1
    assert replayed.state.model_dump(mode="json") == history.restore(repo, stop).data


@pytest.mark.parametrize("target", ["request", "intent", "outside_job", "status"])
def test_unknown_stop_missing_or_untrusted_original_intent_fails(history, tmp_path, target):
    queue, repo, source, base, _, head, environment, _, call, _ = recovered_source(
        history, tmp_path
    )
    if target in ("request", "intent"):
        digest = call["request_digest" if target == "request" else "intent_ref"]
        path = history.objects.path_for(digest)
        assert path.is_relative_to(history.objects.root)
        path.unlink()
    else:
        with history.engine.begin() as connection:
            if target == "outside_job":
                connection.execute(
                    TABLES["jobs"]
                    .update()
                    .where(TABLES["jobs"].c.run_id == source)
                    .values(origin_run_id=source)
                )
            else:
                connection.execute(
                    TABLES["external_calls"]
                    .update()
                    .where(TABLES["external_calls"].c.id == call["id"])
                    .values(status="failed")
                )
    with pytest.raises(AgentGitError) as error:
        RecordingReader(history).load(
            repo,
            RecordedRange(source_run_id=str(source), from_commit=base, until_commit=head),
            expected_environment=environment,
        )
    assert error.value.code in (ErrorCode.RECORDING_MISSING, ErrorCode.REPLAY_MISMATCH)


@pytest.mark.parametrize("target", ["reserved", "signature", "request", "unused"])
def test_unknown_stop_cannot_copy_a_witness_or_omit_a_proof(history, tmp_path, target):
    queue, repo, source, base, _, head, environment, _, _, _ = recovered_source(history, tmp_path)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(source_run_id=str(source), from_commit=base, until_commit=head),
        expected_environment=environment,
    )
    stop = bundle.unknown_stops[0]
    if target == "reserved":
        stop = replace(
            stop, before=evolve(stop.before, usage=evolve(stop.before.usage, reserved_tokens=1))
        )
    elif target == "signature":
        stop = replace(
            stop,
            intent=evolve(
                stop.intent, occurrence=evolve(stop.intent.occurrence, signature_digest="0" * 64)
            ),
        )
    elif target == "request":
        stop = replace(stop, request=b"changed")
    stops = (stop, replace(stop, source_commit=base)) if target == "unused" else (stop,)
    config = queue.research.config(repo, queue.run(repo, source)["agent_config_id"])
    with pytest.raises(AgentGitError) as error:
        RecordedReplayer(history.objects.get).run(
            replace(bundle, unknown_stops=stops), config, queue.research.corpus
        )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH
