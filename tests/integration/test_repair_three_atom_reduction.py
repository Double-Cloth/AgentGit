"""三原子通用协议由真实 S7 配对 Job 执行；不扩大 S9 默认双点空间。"""

import base64
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from diagnosis_evidence import pack_repository_evidence
from test_repair_worker import ConditionalMock, create_case

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.interventions import NewIntervention, PatchProvenance, ToolResponsePatch
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.repair import PhaseQuotas, RepairConsumption, RepairPatch
from agentgit.core.repair_validation import (
    ReductionState,
    RepairAtom,
    RepairCandidate,
    ValidationSample,
    ValidationTrialEvidence,
    validation_ref,
)
from agentgit.core.research import AgentConfig, ModelRequest, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.budget import admission
from agentgit.repair.policy import mock_budget
from agentgit.repair.reduction import (
    lock_reduced_candidate,
    next_deletion,
    record_deletion,
    summarize_reduction,
)
from agentgit.repair.space import patch_ref
from agentgit.repair.templates import TemplateAnchor, model_templates
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService, public
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables
pytestmark = pytest.mark.integration


def fixture_binding():
    return "three_atom_fixture_sha256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ThreeAtomModel(ConditionalMock):
    def invoke(self, payload):
        assert fixture_binding() in parse_contract(payload, ModelRequest).state.goal.text
        return super().invoke(payload)


def prepare_case(history, tmp_path):
    repo, unused_search, research, failed = create_case(
        history, tmp_path, mode="joint", adapter_binding=fixture_binding()
    )
    source = RepairJobService(history).view(repo, unused_search)
    RepairJobService(history).cancel(repo, unused_search, actor="fixture")
    validator = source["manifest"]["space"]["binding"]["validator_ref"]
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == failed.run_id
                )
            ).mappings()
        )
        selected = next(
            row
            for row in calls
            if row["provider"].startswith("tool:") and row["logical_step_id"] == "read-5"
        )
        for row in calls:
            intent = parse_contract(
                history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent
            )
            if not row["provider"].startswith("llm:"):
                continue
            request = parse_contract(
                history.objects.get(intent.request_ref, ObjectType.BLOB), ModelRequest
            )
            ready = request.state.plan.ready(request.state.completed) if request.state.plan else ()
            if request.phase == "act" and ready and ready[0].key == "read-5":
                base, model_call = intent.checkpoint, row
                break
        else:
            pytest.fail("缺少最后一次只读工具之前的模型起点")
        events = TABLES["events"]

        def event_for(row, kind):
            return connection.execute(
                sa.select(events.c.id).where(
                    events.c.run_id == failed.run_id,
                    events.c.logical_step_id == row["logical_step_id"],
                    events.c.type == kind,
                )
            ).scalar_one()

        model_event, tool_event = (
            event_for(model_call, "llm.requested"),
            event_for(selected, "tool.started"),
        )
    manager = ManagementService(research)
    interventions = manager.experiments.interventions
    context = interventions.context(repo, failed.run_id, base, model_event, validator)
    view = VersionService(history).checkout(repo, base)
    config = parse_contract(encode(context["config"]), AgentConfig)
    anchor = TemplateAnchor.model_validate(
        {
            key: context[key]
            for key in (
                "source_run_id",
                "base_commit",
                "target_commit_id",
                "target_event_id",
                "precondition",
            )
        }
    )
    batch = model_templates(
        anchor, config, parse_contract(encode(view["snapshot"]), StateSnapshot), history.objects.get
    )
    prompt = next(p for p in batch.patches if p.template == "prompt-verified-sources/v1")
    plan = next(p for p in batch.patches if p.intervention.operator == "PLAN_PATCH")
    tool = interventions.context(repo, failed.run_id, base, tool_event, validator)
    observed = RepairPatch(
        template="recorded-observation/v1",
        intervention=NewIntervention.model_validate(
            {
                **{
                    key: tool[key]
                    for key in (
                        "source_run_id",
                        "base_commit",
                        "target_commit_id",
                        "target_event_id",
                        "precondition",
                    )
                },
                "operator": "TOOL_RESPONSE_PATCH",
                "payload": ToolResponsePatch(replacement_call_id=str(selected["id"])),
                "effect_policy": "test_only",
                "provenance": PatchProvenance(
                    rationale="C 重复同一完整只读观测，显式作为冗余原子；不伪称外部世界被修复。"
                ),
            }
        ),
    )
    patches = (prompt, plan, observed)
    identities = {
        patch_ref(p): manager.create_intervention(repo, p.intervention, f"three-{i}")[
            "intervention_id"
        ]
        for i, p in enumerate(patches)
    }
    # S7 每项配对占两个实际 trial；原先冻结十二项额度，绝不运行后追补。
    budget = evolve(
        mock_budget(config),
        quotas=PhaseQuotas(screen=8, reduce=12, confirm=6, regression=4),
        max_concurrency=2,
        max_tokens=6_000_000,
    )
    protocol = dict(
        schema_version=1,
        kind="three-atom-physical-reduction/v1",
        fixture=fixture_binding(),
        source_run_id=str(failed.run_id),
        base_commit=base,
        validator_ref=validator,
        budget=budget.model_dump(mode="json"),
        ordered_patches=[p.model_dump(mode="json") for p in patches],
        policy="配对前置对照与删除试验均计入 reduce；C 为同响应只读观测冗余。",
    )
    binding = history.register_input(repo, encode(protocol))
    candidate = RepairCandidate(
        binding_ref=binding,
        source_proposal_ref=binding,
        task_ref=manager.experiments.validators.manifest(repo, validator).input_ref,
        atoms=tuple(
            RepairAtom(patch_ref=patch_ref(p), operator=p.intervention.operator) for p in patches
        ),
        model_scope="deterministic_fixture",
    )
    policy = HybridPolicy(
        until_commit=failed.head, calls=(str(selected["id"]),), miss_policy="stop"
    )
    return repo, research, failed, manager, validator, base, budget, candidate, identities, policy


def test_real_three_atom_deletion_removes_c_and_retests_a_b_under_shared_frozen_budget(
    history, tmp_path
):
    repo, research, failed, manager, validator, base, budget, initial, identities, hybrid = (
        prepare_case(history, tmp_path)
    )
    original = history.branch(repo)
    with history.engine.connect() as connection:
        began = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
    consumption, reports, admissions = RepairConsumption(), [], []
    worker = Worker(JobService(research), model_factory=ThreeAtomModel)

    def pair(before, after):
        nonlocal consumption
        with history.engine.connect() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        consumption = evolve(consumption, elapsed_wall_ms=int((now - began).total_seconds() * 1000))
        for _ in range(2):
            decision = admission(budget, consumption, "reduce")
            assert decision.allowed, decision
            admissions.append(decision.model_dump(mode="json"))
            consumption = evolve(
                consumption,
                active=consumption.active + 1,
                started=evolve(consumption.started, reduce=consumption.started.reduce + 1),
            )
        request = NewExperiment(
            source_run_id=str(failed.run_id),
            from_commit=base,
            validator_ref=validator,
            groups=tuple(
                ExperimentGroup(
                    key=key,
                    label=key,
                    interventions=tuple(identities[a.patch_ref] for a in value.atoms),
                )
                for key, value in (("A", before), ("B", after))
            ),
            repeats=1,
            budget=budget.trial,
            hybrid=hybrid,
        )
        submitted = manager.create_experiment(repo, request, f"physical-pair-{len(reports)}")
        for _ in range(2):
            assert worker.run_once()["status"] in ("completed", "failed")
        report = manager.experiment(repo, UUID(submitted["experiment_id"]))
        for trial, candidate in zip(report["trials"], (before, after), strict=True):
            usage = RepairTrialService._usage(trial["replay"])
            assert usage is not None and usage.model_calls > 0
            assert trial["assessment"] is not None and trial["verdict"] in ("passed", "failed")
            assert len(trial["replay"]["observations"]) == 1
            observation = trial["replay"]["observations"][0]
            has_c = initial.atoms[2] in candidate.atoms
            assert observation["kind"] == ("synthetic" if has_c else "recorded")
            assert observation.get("intervention_id") == (
                identities[initial.atoms[2].patch_ref] if has_c else None
            )
            consumption = evolve(
                consumption,
                active=consumption.active - 1,
                **{
                    name: getattr(consumption, name) + getattr(usage, name)
                    for name in ("model_calls", "tool_calls", "tokens", "cost_micro_usd")
                },
            )
        reports.append(report)
        with history.engine.connect() as connection:
            now = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
        consumption = evolve(consumption, elapsed_wall_ms=int((now - began).total_seconds() * 1000))
        return report["trials"]

    baseline, selected = pair(evolve(initial, atoms=()), initial)
    assert baseline["verdict"] == "failed" and selected["verdict"] == "passed"
    origin_ref = history.register_input(repo, encode(reports[0]))
    state = ReductionState(
        initial=initial,
        sample=ValidationSample(
            role="reduce",
            task_ref=initial.task_ref,
            response_set_ref=origin_ref,
            validator_ref=validator,
        ),
        screening_run_ids=(str(failed.run_id), selected["run_id"]),
        screening_result_refs=(origin_ref,),
        max_trials=5,
    )
    assert history.register_input(repo, encode(state.model_dump(mode="json"))) == validation_ref(
        state
    )
    results = []
    while (deletion := next_deletion(state)) is not None:
        before = summarize_reduction(state).final_candidate
        control, trial = pair(before, deletion.after)
        assert control["verdict"] == "passed"
        result_ref = history.register_input(repo, encode(trial))
        with history.engine.connect() as connection:
            table = TABLES["experiment_trials"]
            stored = (
                connection.execute(sa.select(table).where(table.c.job_id == UUID(trial["job_id"])))
                .mappings()
                .one()
            )
        assert str(stored["experiment_id"]) == reports[-1]["experiment_id"]
        assert stored["group_key"] == trial["group"] and stored["trial_index"] == trial["index"]
        evidence = ValidationTrialEvidence(
            phase="reduce",
            protocol_ref=validation_ref(state),
            # S7 trial 的复合键以唯一 Job UUID 关联到缩减协议，不虚构另一张 trial 表。
            trial_id=trial["job_id"],
            run_id=trial["run_id"],
            result_ref=result_ref,
            candidate_ref=validation_ref(deletion.after),
            sample_ref=deletion.sample_ref,
            validator_ref=validator,
            verdict=trial["verdict"],
            category="oracle",
            usage=RepairTrialService._usage(trial["replay"]),
        )
        results.append(evidence.verdict)
        state = record_deletion(state, evidence)
        assert history.register_input(
            repo, encode(state.model_dump(mode="json"))
        ) == validation_ref(state)
    summary = summarize_reduction(state)
    assert results == ["failed", "failed", "passed", "failed", "failed"]
    assert summary.status == "complete" and summary.minimality == "one_minimal_under_fixture"
    assert summary.final_candidate.atoms == initial.atoms[:2] and summary.removed_atoms == 1
    assert summary.field_reduction == "not_run" and not summary.global_optimum
    all_trials = [trial for report in reports for trial in report["trials"]]
    all_result_refs = tuple(history.register_input(repo, encode(t)) for t in all_trials)
    event_ref = history.register_input(
        repo,
        encode(
            dict(
                schema_version=1, state_ref=validation_ref(state), result_refs=list(all_result_refs)
            )
        ),
    )
    lock = lock_reduced_candidate(state, selection_event_ref=event_ref)
    # 配对的前置对照也已经可见，在封存前全部加入排除范围，不能留给未来确认充数。
    lock = evolve(
        lock,
        prior_run_ids=tuple(
            dict.fromkeys((*lock.prior_run_ids, *(t["run_id"] for t in all_trials)))
        ),
        selection_result_refs=tuple(dict.fromkeys((*lock.selection_result_refs, *all_result_refs))),
    )
    for value in (summary, lock):
        assert history.register_input(
            repo, encode(value.model_dump(mode="json"))
        ) == validation_ref(value)
    assert lock.candidate == summary.final_candidate
    assert consumption.started.reduce == 12 and consumption.active == 0
    assert len({t["job_id"] for t in all_trials}) == len({t["run_id"] for t in all_trials}) == 12
    assert all(t["run_id"] != str(failed.run_id) for t in all_trials)
    assert worker.run_once() is None and history.branch(repo) == original
    source_graph = BehaviorService(history).trace(repo, failed.run_id).model_dump(mode="json")
    raw = pack_repository_evidence(
        history,
        dict(
            case_id="S10-three-atom-physical-reduction/v1",
            repository_id=str(repo),
            failed_run_id=str(failed.run_id),
            reference_run_id=str(failed.run_id),
            graph=source_graph,
            reference_graph=source_graph,
        ),
    )
    with history.engine.connect() as connection:
        indexes = {
            name: [
                public(dict(row)) for row in connection.execute(sa.select(TABLES[name])).mappings()
            ]
            for name in ("jobs", "replay_requests", "experiments", "experiment_trials")
        }
    contents = {
        ref: base64.b64decode(item["base64"], validate=True) for ref, item in raw["objects"].items()
    }
    measured = dict(model_calls=0, tool_calls=0, tokens=0, cost_micro_usd=0)
    for trial in all_trials:
        rows = [
            row
            for row in raw["indexes"]["external_calls"]
            if row["run_id"] == trial["run_id"] and row["origin_call_id"] is None
        ]
        models = [row for row in rows if row["provider"].startswith("llm:")]
        observations = {item["call_id"] for item in trial["replay"]["observations"]}
        tools = [
            row
            for row in rows
            if row["provider"].startswith("tool:") and row["id"] not in observations
        ]
        byte_count = 0
        for call in models:
            intent = json.loads(contents[call["intent_ref"]])
            outcome = json.loads(contents[call["result_ref"]])
            assert outcome["complete"] and outcome["error"] is None
            byte_count += len(contents[intent["request_ref"]]) + len(
                contents[outcome["response_ref"]]
            )
        usage = RepairTrialService._usage(trial["replay"])
        assert usage.model_calls == len(models) and usage.tool_calls == len(tools)
        assert usage.tokens == byte_count and usage.unit == "mock_utf8_bytes"
        for name in measured:
            measured[name] += getattr(usage, name)
    assert measured == {name: getattr(consumption, name) for name in measured}
    bundle = dict(
        schema_version=1,
        protocol_ref=initial.binding_ref,
        budget=budget.model_dump(mode="json"),
        consumption=consumption.model_dump(mode="json"),
        admissions=admissions,
        reports=reports,
        state=state.model_dump(mode="json"),
        summary=summary.model_dump(mode="json"),
        lock=lock.model_dump(mode="json"),
        indexes=indexes,
        raw_repository=raw,
    )
    destination = os.environ.get("AGENTGIT_S10_THREE_ATOM_EVIDENCE")
    if destination:
        with Path(destination).open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(bundle, handle, ensure_ascii=False, indent=2)
