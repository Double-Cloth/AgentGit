"""真实 Job/Worker 和供应商 Adapter 接线；HTTP 使用离线契约替身，不声称模型质量。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.model_http import HTTPReply, TransportFailure
from agentgit.adapters.model_protocol import configured_model_signature
from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.model_usage import ModelMeter, ModelReply, OpenAIOptions
from agentgit.core.models import CallIntent, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetLimits, ResearchGoal, evolve
from agentgit.core.versions import LiveOverrides, LiveReplay, RecordedRange, RecordedReplay
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
MODEL = "openai/gpt-5.4-mini-2026-03-17"
PUBLIC_KEY = "sk-agentgit-public-offline-runtime-fixture-only"


class ProcessLost(BaseException):
    pass


class FixtureHTTP:
    def __init__(self, mode="ok"):
        self.mode, self.calls, self.generations, self.receipts = mode, [], 0, {}
        self.count_client = None
        self.block_started, self.block_release = Event(), Event()

    def post(self, path, body, **options):
        self.calls.append((path, body, options))
        if path.endswith("input_tokens"):
            self.count_client = options["client_request_id"]
            return HTTPReply(
                200, encode({"object": "response.input_tokens", "input_tokens": 100}), "req_count"
            )
        self.generations += 1
        if self.mode == "rate_always" or self.mode == "rate_once" and self.generations == 1:
            return HTTPReply(429, b"{}", "req_rate", "0")
        original = encode(json.loads(json.loads(body)["input"]))
        answer = decode(MockResearchModel().invoke(original))
        structured = {
            "kind": answer["kind"],
            "plan": answer.get("plan"),
            "node_key": answer.get("node_key"),
            "tool": answer.get("tool"),
            "arguments_json": json.dumps(answer["arguments"], ensure_ascii=False)
            if "arguments" in answer
            else None,
            "summary": answer.get("summary"),
        }
        meter = ModelMeter(
            basis="provider_usage",
            counted_input_tokens=100,
            input_tokens=100,
            output_tokens=20,
            cached_tokens=40,
            cache_write_tokens=0,
            reasoning_tokens=5,
            price_verified=True,
            accounted_input_tokens=100,
            accounted_output_tokens=20,
            estimated_cost_micro_usd=138,
        )
        self.receipts[original] = encode(
            ModelReply(
                disposition="decision",
                decision=answer,
                error_code=None,
                meter=meter,
                response_id="resp_fixture",
                request_id="req_generated",
                count_request_id="req_count",
                client_request_id=options["client_request_id"],
                count_client_request_id=self.count_client,
                http_status=200,
                retry_after_ms=None,
            ).model_dump(mode="json")
        )
        if self.mode == "unknown" and self.generations == 1:
            raise TransportFailure(sent=True)
        if self.mode == "crash" and self.generations == 1:
            raise ProcessLost()
        if self.mode == "block" and self.generations == 1:
            self.block_started.set()
            assert self.block_release.wait(15)
        payload = {
            "id": "resp_fixture",
            "object": "response",
            "model": OpenAIOptions().model,
            "service_tier": "priority" if self.mode == "unpriced" else "default",
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "usage": None
            if self.mode == "missing_usage"
            else {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 5},
            },
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": json.dumps(structured)}],
                }
            ],
        }
        return HTTPReply(200, json.dumps(payload).encode(), "req_generated")


def setup(history, tmp_path, monkeypatch, *, mode="ok", limits=None):
    transport = FixtureHTTP(mode)
    monkeypatch.setenv("OPENAI_API_KEY", PUBLIC_KEY)
    monkeypatch.setattr("agentgit.adapters.openai.FixedOpenAIHTTPS", lambda: transport)
    service = ResearchService(history, tmp_path / "provider-runtime")
    repo = history.create_repository("供应商协议接线验收")
    config = AgentConfig(
        model=MODEL,
        openai=OpenAIOptions(),
        limits=limits
        or BudgetLimits(
            max_cost_micro_usd=1_000_000,
            max_wall_ms=120_000,
        ),
    )
    config_id = service.create_config(repo, config)
    task = service.create_task(repo, config_id, ResearchGoal(text="比较合成 fixture 框架"))
    queue = JobService(service)
    submitted = queue.enqueue(
        repo,
        task,
        actor="fixture",
        key="source",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    return service, repo, config, queue, submitted, transport


def snapshot(history, repo, queue, submitted):
    row = queue.get(repo, UUID(submitted.job_id))
    return parse_contract(
        encode(history.restore(repo, row["checkpoint_commit_id"]).data), AgentState
    )


def commits(history, repo):
    return tuple(item["commit_id"] for item in reversed(VersionService(history).log(repo)["items"]))


def no_execution(*_args, **_kwargs):
    pytest.fail("Recorded 不得构造模型或触及网络/工具")


def test_default_worker_selects_adapter_and_recorded_recomputes_meter_without_client(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, config, queue, submitted, http = setup(history, tmp_path, monkeypatch)
    assert Worker(queue).run_once()["status"] == "completed"
    final = snapshot(history, repo, queue, submitted)
    assert http.generations == final.usage.model_calls == 11 and len(http.calls) == 22
    assert final.usage.tokens == 1320 and final.usage.cost_micro_units == 1518
    assert final.usage.usage_kind == "provider_tokens"
    assert final.usage.model_meter.measured_calls == 11
    assert final.usage.model_meter.reserved_cost_micro_usd == 0
    with history.engine.connect() as connection:
        calls = list(connection.execute(sa.select(TABLES["external_calls"])).mappings())
    model_calls = [row for row in calls if row["provider"] == "llm:" + MODEL]
    assert len(model_calls) == 11
    for row in model_calls:
        intent = parse_contract(history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent)
        assert intent.model_budget.tokens == 40_960
        assert intent.model_budget.cost_micro_usd == 61_440
    points = commits(history, repo)
    manager = ManagementService(service)
    request = RecordedReplay(
        source_run_id=submitted.run_id,
        from_commit=points[0],
        until_commit=points[-1],
        branch_name="metered-recorded",
    )
    replay = manager.replay(repo, request, "recorded")
    before = history.branch(repo)
    with monkeypatch.context() as offline:
        offline.delenv("OPENAI_API_KEY")
        for target in (
            "agentgit.adapters.models.select_model",
            "agentgit.worker.select_model",
            "agentgit.adapters.openai.OpenAIResearchModel.__init__",
            "agentgit.adapters.tools.ToolRegistry.invoke",
            "socket.create_connection",
            "agentgit.runtime.engine.time.sleep",
        ):
            offline.setattr(target, no_execution)
        assert Worker(manager.jobs).run_once()["status"] == "completed"
    projected = history.restore(
        repo, manager.jobs.run(repo, UUID(replay["run_id"]))["checkpoint_commit_id"]
    )
    assert projected.data == final.model_dump(mode="json") and history.branch(repo) == before
    report = manager.replay_status(repo, UUID(replay["job_id"]))["replay"]
    assert report["verified"] and report["actual"]["model_calls"] == 0
    assert report["actual"]["cost_micro_units"] == 0
    for path in (tmp_path / "objects").rglob("*"):
        if path.is_file():
            assert PUBLIC_KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize(
    "mode,status,calls",
    [
        ("rate_once", "completed", 12),
        ("rate_always", "failed", 3),
        ("missing_usage", "failed", 1),
        ("unpriced", "failed", 1),
    ],
)
def test_retry_stop_and_unknown_usage_are_bounded_and_replayable(
    history,
    tmp_path,
    monkeypatch,
    mode,
    status,
    calls,
):
    service, repo, config, queue, submitted, http = setup(
        history,
        tmp_path,
        monkeypatch,
        mode=mode,
    )
    assert Worker(queue).run_once()["status"] == status
    state = snapshot(history, repo, queue, submitted)
    assert state.usage.model_calls == http.generations == calls
    if mode == "missing_usage":
        assert state.usage.model_meter.estimated_calls == 1
        assert state.usage.tokens == 8292 and state.usage.cost_micro_units == 36_939
    elif mode == "unpriced":
        assert state.usage.cost_micro_units == 0
        assert state.usage.model_meter.unpriced_cost_micro_usd == 61_440
    elif mode == "rate_always":
        assert state.usage.replans == 2 and state.usage.cost_micro_units == 0
    points = commits(history, repo)
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(
            source_run_id=submitted.run_id,
            from_commit=points[0],
            until_commit=points[-1],
        ),
        expected_environment=parse_contract(
            encode(VersionService(history).checkout(repo, points[0])["environment"]),
            EnvironmentManifest,
        ),
    )
    with monkeypatch.context() as offline:
        offline.setattr("agentgit.runtime.engine.time.sleep", no_execution)
        offline.setattr("agentgit.adapters.openai.OpenAIResearchModel.__init__", no_execution)
        result = RecordedReplayer(history.objects.get).run(bundle, config, service.corpus)
    assert result.state == state and result.actual_model_calls == 0
    if mode == "rate_once":
        first = bundle.recordings[0]
        altered = replace(
            first,
            intent=evolve(
                first.intent,
                model_budget=evolve(
                    first.intent.model_budget,
                    cost_micro_usd=1,
                ),
            ),
        )
        with pytest.raises(AgentGitError) as mismatch:
            RecordedReplayer(history.objects.get).run(
                replace(bundle, recordings=(altered, *bundle.recordings[1:])),
                config,
                service.corpus,
            )
        assert mismatch.value.code == ErrorCode.REPLAY_MISMATCH


def test_money_budget_blocks_next_intent_and_api_reports_usd_estimate(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, _, queue, submitted, http = setup(
        history,
        tmp_path,
        monkeypatch,
        limits=BudgetLimits(
            max_cost_micro_usd=61_440,
        ),
    )
    assert Worker(queue).run_once()["status"] == "budget_exhausted"
    assert http.generations == 1
    state = snapshot(history, repo, queue, submitted)
    assert state.usage.cost_micro_units == 138 and state.usage.model_calls == 1
    result = ManagementService(service).run(repo, UUID(submitted.run_id))
    assert result["budget"]["currency"] == "USD"
    assert result["budget"]["cost_basis"] == "pricebook_estimate"
    assert result["budget"]["remaining"]["cost_micro_usd"] == 61_302


def test_live_uses_new_provider_budget_and_keeps_source_unchanged(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, config, queue, submitted, http = setup(history, tmp_path, monkeypatch)
    # 先完成真实协议来源，再从其安全非终态检查点使用更小的独立预算。
    assert Worker(queue).run_once()["status"] == "completed"
    original = history.branch(repo)
    point = commits(history, repo)[8]
    before = parse_contract(encode(history.restore(repo, point).data), AgentState)
    manager = ManagementService(service)
    replay = manager.replay(
        repo,
        LiveReplay(
            source_run_id=submitted.run_id,
            from_commit=point,
            branch_name="metered-live",
            budget=BudgetLimits(max_cost_micro_usd=100_000),
            overrides=LiveOverrides(),
        ),
        "live",
    )
    count_before = http.generations
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    report = manager.replay_status(repo, UUID(replay["job_id"]))["replay"]
    new = http.generations - count_before
    assert report["new_usage"]["model_calls"] == new > 0
    assert report["new_usage"]["cost_micro_units"] == new * 138
    assert report["actual"]["currency"] == "USD"
    assert report["actual"]["cost_basis"] == "pricebook_estimate"
    assert report["budget_baseline"] == before.usage.model_dump(mode="json")
    assert history.branch(repo) == original


def test_sent_unknown_retains_reservation_and_trusted_receipt_settles_once(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, config, queue, submitted, http = setup(
        history, tmp_path, monkeypatch, mode="unknown"
    )
    assert Worker(queue).run_once()["status"] == "outcome_unknown"
    unknown = snapshot(history, repo, queue, submitted)
    assert unknown.usage.tokens == unknown.usage.cost_micro_units == 0
    assert unknown.usage.reserved_tokens == 40_960
    assert unknown.usage.model_meter.reserved_cost_micro_usd == 61_440
    with history.engine.connect() as connection:
        call = connection.execute(sa.select(TABLES["external_calls"])).mappings().one()

    class Resolver:
        provider = "llm:" + MODEL
        signature = configured_model_signature(config)

        def resolve(self, request):
            return VerifiedOutcome(http.receipts[request], b"public-offline-provider-receipt")

    assert Worker(queue).run_once() is None and http.generations == 1
    run = UUID(submitted.run_id)
    ReconciliationService(history).confirm(
        repo,
        run,
        call["id"],
        actor="fixture",
        expected_version=queue.run(repo, run)["state_version"],
        resolver=Resolver(),
    )
    assert Worker(queue).run_once()["status"] == "completed"
    done = snapshot(history, repo, queue, submitted)
    assert done.usage.model_calls == http.generations == 11
    assert done.usage.tokens == 1320 and done.usage.cost_micro_units == 1518
    assert done.usage.model_meter.reserved_cost_micro_usd == done.usage.reserved_tokens == 0


def test_inflight_intent_reservation_is_visible_before_reply_and_not_double_counted(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, _, queue, submitted, http = setup(history, tmp_path, monkeypatch, mode="block")
    manager = ManagementService(service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(Worker(queue).run_once)
        try:
            assert http.block_started.wait(10)
            budget = manager.run(repo, UUID(submitted.run_id))["budget"]
            assert budget["pending_model_reservation"] == {
                "call_count": 1,
                "tokens": 40_960,
                "cost_micro_usd": 61_440,
            }
            assert budget["remaining"]["cost_micro_usd"] == 938_560
            assert budget["remaining"]["tokens"] == 959_040
        finally:
            http.block_release.set()
        assert future.result(timeout=30)["status"] == "completed"
    budget = manager.run(repo, UUID(submitted.run_id))["budget"]
    assert budget["pending_model_reservation"]["call_count"] == 0
    assert budget["remaining"]["cost_micro_usd"] == 998_482


def test_worker_takeover_rebuilds_unknown_money_from_durable_intent_without_resend(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, config, queue, submitted, http = setup(
        history, tmp_path, monkeypatch, mode="crash"
    )
    with pytest.raises(ProcessLost):
        Worker(queue).run_once()
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(submitted.job_id))
            .values(lease_until=sa.func.clock_timestamp() - sa.text("interval '1 second'"))
        )
    sealed = Worker(queue).run_once()
    assert sealed["status"] == "outcome_unknown" and sealed["run_id"] != submitted.run_id
    state = snapshot(history, repo, queue, submitted)
    assert state.usage.model_calls == http.generations == 1
    assert state.usage.model_meter.reserved_cost_micro_usd == 61_440
    assert state.usage.reserved_tokens == 40_960
    current = UUID(sealed["run_id"])
    base = queue.run(repo, current)["base_commit_id"]
    stop = queue.get(repo, UUID(submitted.job_id))["checkpoint_commit_id"]
    environment = parse_contract(
        encode(VersionService(history).checkout(repo, base)["environment"]), EnvironmentManifest
    )
    bundle = RecordingReader(history).load(
        repo,
        RecordedRange(
            source_run_id=str(current),
            from_commit=base,
            until_commit=stop,
        ),
        expected_environment=environment,
    )
    with monkeypatch.context() as offline:
        offline.setattr("agentgit.adapters.openai.OpenAIResearchModel.__init__", no_execution)
        rebuilt = RecordedReplayer(history.objects.get).run(bundle, config, service.corpus)
    assert rebuilt.state == state and rebuilt.actual_model_calls == 0
    with history.engine.connect() as connection:
        call = connection.execute(sa.select(TABLES["external_calls"])).mappings().one()

    class Resolver:
        provider = "llm:" + MODEL
        signature = configured_model_signature(config)

        def resolve(self, request):
            return VerifiedOutcome(http.receipts[request], b"public-offline-crash-receipt")

    ReconciliationService(history).confirm(
        repo,
        current,
        call["id"],
        actor="fixture",
        expected_version=queue.run(repo, current)["state_version"],
        resolver=Resolver(),
    )
    assert Worker(queue).run_once()["status"] == "completed"
    done = snapshot(history, repo, queue, submitted)
    assert done.usage.model_calls == http.generations == 11
    assert done.usage.cost_micro_units == 1518


def test_live_switch_from_mock_to_provider_keeps_mixed_history_and_independent_budget(
    history,
    tmp_path,
    monkeypatch,
):
    http = FixtureHTTP()
    monkeypatch.setenv("OPENAI_API_KEY", PUBLIC_KEY)
    monkeypatch.setattr("agentgit.adapters.openai.FixedOpenAIHTTPS", lambda: http)
    service = ResearchService(history, tmp_path / "mixed-provider-live")
    repo = history.create_repository("Mock 与 Token 历史切换")
    config_id = service.create_config(repo)
    task = service.create_task(repo, config_id, ResearchGoal(text="比较合成资料"))
    source = service.start(repo, task)
    source.execute()
    original = history.branch(repo)
    point = commits(history, repo)[8]
    baseline = parse_contract(encode(history.restore(repo, point).data), AgentState).usage
    provider_config = AgentConfig(
        model=MODEL,
        openai=OpenAIOptions(),
        limits=BudgetLimits(
            max_cost_micro_usd=100_000,
        ),
    )
    manager = ManagementService(service)
    replay = manager.replay(
        repo,
        LiveReplay(
            source_run_id=str(source.run_id),
            from_commit=point,
            branch_name="mixed-live",
            overrides=LiveOverrides(config=provider_config),
        ),
        "mixed",
    )
    assert Worker(manager.jobs).run_once()["status"] == "completed"
    report = manager.replay_status(repo, UUID(replay["job_id"]))["replay"]
    assert report["logical_usage"]["usage_kind"] == "mixed_units"
    assert report["new_usage"]["tokens"] == 120 * http.generations
    assert report["new_usage"]["cost_micro_units"] == 138 * http.generations
    assert report["logical_usage"]["tokens"] == baseline.tokens + 120 * http.generations
    assert history.branch(repo) == original


@pytest.mark.parametrize(
    "limits",
    [
        BudgetLimits(max_cost_micro_usd=0),
        BudgetLimits(max_cost_micro_usd=100_000, max_response_bytes=1024),
    ],
)
def test_invalid_effective_run_budget_is_rejected_before_job_creation(history, tmp_path, limits):
    service = ResearchService(history, tmp_path / "invalid-provider-budget")
    repo = history.create_repository("预算收紧后的配置仍须合法")
    config = AgentConfig(
        model=MODEL,
        openai=OpenAIOptions(),
        limits=BudgetLimits(
            max_cost_micro_usd=100_000,
        ),
    )
    config_id = service.create_config(repo, config)
    task = service.create_task(repo, config_id, ResearchGoal(text="不得进入无效 Worker 配置"))
    queue = JobService(service)
    with pytest.raises(AgentGitError) as error:
        queue.enqueue(
            repo,
            task,
            actor="fixture",
            key="invalid",
            config_version=1,
            expected_branch_version=0,
            expected_head=None,
            budget=limits,
        )
    assert error.value.code == ErrorCode.VALIDATION
    with history.engine.connect() as connection:
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(TABLES["jobs"])).scalar() == 0
        )


def test_invalid_effective_live_budget_returns_validation_before_creating_branch(
    history,
    tmp_path,
    monkeypatch,
):
    service, repo, _, queue, submitted, _ = setup(history, tmp_path, monkeypatch)
    assert Worker(queue).run_once()["status"] == "completed"
    manager = ManagementService(service)
    with pytest.raises(AgentGitError) as error:
        manager.replay(
            repo,
            LiveReplay(
                source_run_id=submitted.run_id,
                from_commit=commits(history, repo)[8],
                branch_name="invalid-live",
                budget=BudgetLimits(max_cost_micro_usd=0),
            ),
            "invalid-live",
        )
    assert error.value.code == ErrorCode.VALIDATION
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(TABLES["branches"].c.name).where(
                    TABLES["branches"].c.name == "invalid-live"
                )
            ).first()
            is None
        )
