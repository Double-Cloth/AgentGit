"""基线执行接线冒烟，使用旧工程例验证流程，明确不计入新的 30 例。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import repair_benchmark_protocol as protocol
import sqlalchemy as sa
from confirmation_evidence import capture_confirmation
from repair_case_execution import drive
from test_repair_confirmation import ConfirmationMock, adapter_binding
from test_repair_worker import create_case

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.models import parse_contract
from agentgit.core.repair import RepairSearchManifest
from agentgit.core.repair_pools import RegressionOrigin
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ResearchGoal
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_pools import RepairValidationPoolService
from agentgit.services.repair_searches import RepairSearchService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables


def policy():
    return (
        "benchmark-method-smoke/v1;protocol-sha256="
        + hashlib.sha256(Path(protocol.__file__).read_bytes()).hexdigest()
        + ";driver-sha256="
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )


def first_local_context(history, graph):
    # 这是公开轨迹上的局部时间启发式，不声称相邻请求就是故障原因。
    nodes = sorted(graph.nodes, key=lambda node: node.event.seq)
    for node in nodes:
        if not node.payload_verified:
            continue
        data = history.objects.get_json(node.payload_ref, ObjectType.EVENT)["data"]
        if (
            node.error_code is not None
            or isinstance(data.get("error"), dict)
            and data["error"].get("code")
            or isinstance(data.get("evaluation"), dict)
            and data["evaluation"].get("passed") is False
        ):
            requests = [
                before
                for before in nodes
                if before.event.seq < node.event.seq
                and before.payload_verified
                and before.event_type == "llm.requested"
                and before.occurrence is not None
                and before.occurrence.provider.startswith("llm:")
            ]
            target = requests[-1].event if requests else None
            return dict(
                first_exception=node.event.model_dump(mode="json"),
                local_request=target.model_dump(mode="json") if target else None,
                target_event_id=target.event_id if target else None,
            )
    return dict(first_exception=None, local_request=None, target_event_id=None)


def prepare_origins(research, repo, config):
    origins = []
    for index in range(4):
        task = research.create_task(
            repo,
            config,
            ResearchGoal(text=f"regression-{index};{adapter_binding(policy())};固定框架语义回归"),
        )
        run = research.start(repo, task)
        assert run.execute(ConfirmationMock(policy())).status.value == "completed"
        with research.history.engine.connect() as connection:
            links = TABLES["run_commits"]
            first = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        origins.append(RegressionOrigin(source_run_id=str(run.run_id), from_commit=first))
    return tuple(origins)


def execute_smoke(history, tmp_path, method):
    repo, legacy, research, failed = create_case(
        history, tmp_path, mode="prompt", adapter_binding=adapter_binding(policy())
    )
    ledger = RepairJobService(history)
    original = parse_contract(encode(ledger.view(repo, legacy)["manifest"]), RepairSearchManifest)
    ledger.cancel(repo, legacy, actor="fixture")
    graph = BehaviorService(history).trace(repo, failed.run_id)
    local = first_local_context(history, graph)
    planned = protocol.method_plan(
        method,
        original.space,
        original.budget,
        original.intervention_ids,
        first_exception_target=local["target_event_id"],
    )
    with history.engine.connect() as connection:
        config = history._run(connection, repo, failed.run_id)["agent_config_id"]
    origins = prepare_origins(research, repo, config)
    created = RepairSearchService(history).submit(
        planned.space,
        planned.budget,
        planned.intervention_ids,
        actor="fixture",
        key="benchmark-smoke",
        _regression=origins,
        _research=research,
    )
    search = UUID(created["search_id"])
    workflow = ManagementService(research).repair_workflow(repo, search)
    assert workflow["suite_id"]
    runner = Worker(JobService(research), model_factory=lambda: ConfirmationMock(policy()))
    source = drive(research, repo, search, runner, "screen")
    result = dict(
        method=method,
        scope="legacy-engineering-fixture/method-wiring-only",
        method_ref=planned.identity(),
        common_budget_ref=protocol.common_budget_ref(planned.budget),
        original_space=original.space.model_dump(mode="json"),
        selected_space=planned.space.model_dump(mode="json"),
        budget=planned.budget.model_dump(mode="json"),
        local_context=local,
        source=source,
        source_before=graph.model_dump(mode="json"),
        status=source["status"],
    )
    if source["status"] == "needs_confirmation":
        made = RepairStudyService(history).submit(
            repo,
            NewRepairStudy(
                search_id=str(search),
                max_reduce_trials=planned.max_reduce_trials,
                field_policy=planned.field_policy,
            ),
            actor="fixture",
            key="study",
        )
        study = UUID(made["study_id"])
        reduced = drive(research, repo, study, runner, "reduce")
        assert reduced["status"] == "awaiting_samples"
        assert RepairValidationPoolService(research).open(
            repo, study, UUID(workflow["suite_id"]), actor="fixture", key="open"
        )["opened"]
        final = drive(research, repo, study, runner, "confirm")
        result.update(status=final["status"], reduced=reduced, validation=final)
        result["archive"] = capture_confirmation(
            history,
            final,
            source,
            reduced,
            case=f"benchmark-method-smoke/{method}/v1",
            environment="AGENTGIT_UNUSED_SMOKE_EXPORT",
        )
    result["source_after"] = (
        BehaviorService(history).trace(repo, failed.run_id).model_dump(mode="json")
    )
    assert result["source_before"] == result["source_after"]
    directory = os.environ.get("AGENTGIT_S10_BENCHMARK_SMOKE_DIR")
    if directory:
        folder = Path(directory).resolve()
        assert folder.parent == (Path.cwd() / ".agentgit/evidence").resolve()
        assert folder.name.startswith("S10-benchmark-method-smoke-")
        folder.mkdir(exist_ok=True)
        with (folder / f"{method}.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
    return result
