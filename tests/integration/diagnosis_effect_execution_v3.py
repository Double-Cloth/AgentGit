"""在原二十例同一故障条件中，经真实候选/S7/Worker 运行固定单变量 A/B。"""

import hashlib
import json
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from diagnosis_dataset import DATA, Faults, FixtureModel, load_dataset
from diagnosis_effect_managed_v3 import run_case
from diagnosis_effect_policy import (
    MAX_CONTEXTS,
    POLICY,
    PROMPT_SUFFIX,
    fixed_payload,
    select_context,
)
from diagnosis_evidence import pack_repository_evidence

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.interventions import NewIntervention
from agentgit.core.research import BudgetLimits, ResearchGoal, evolve
from agentgit.diagnosis.candidates import RULES_DIGEST
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService, public
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = DATA.parent / "effects-v3.json"
OUTPUT = ROOT / ".agentgit/evidence/S8-candidate-effects-v3"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protocol_value():
    dataset = load_dataset()
    rules = json.loads(
        (ROOT / "doc/acceptance/S8-static-rules-v1-storage-2.json").read_text(encoding="utf-8")
    )
    assert rules["rules_digest"] == RULES_DIGEST
    assert all(sha(ROOT / name) == value for name, value in rules["implementation_sha256"].items())
    frozen = [
        "tests/integration/diagnosis_dataset.py",
        "tests/integration/diagnosis_evidence.py",
        "tests/integration/diagnosis_effect_policy.py",
        "tests/integration/diagnosis_effect_execution_v3.py",
        "tests/integration/diagnosis_effect_managed_v3.py",
        "tests/integration/test_diagnosis_candidate_effects_v3.py",
        "tests/conftest.py",
        "tests/fixtures/diagnosis/v1/cases.json",
        "tests/fixtures/diagnosis/v1/release.json",
        "doc/acceptance/S8-static-rules-v1-storage-2.json",
        "alembic.ini",
        "pyproject.toml",
        "uv.lock",
    ]
    assert (ROOT / "migrations").is_dir()
    frozen += [
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / "migrations").rglob("*"))
        if p.suffix in (".py", ".mako")
    ]
    return dict(
        kind="diagnosis-candidate-effects/v3",
        source_kind="durable-sequential-tasks/full-frozen-corpus",
        scope="engineering-supplement/previously-seen-twenty",
        source_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        files={name: sha(ROOT / name) for name in frozen},
        rules_digest=RULES_DIGEST,
        cases=[case.id for case in dataset.cases],
        policy=POLICY,
        max_context_attempts=MAX_CONTEXTS,
        prompt_suffix=PROMPT_SUFFIX,
        experiments_per_case=1,
        repeats_per_group=5,
        trial_budget=evolve(dataset.limits, max_cost_micro_usd=0).model_dump(mode="json"),
        faults_enabled_in_both_groups=True,
        rerank=False,
        human_review="pending_human",
        partial_or_incomplete_effect="unknown",
    )


def load_protocol():
    payload = PROTOCOL.read_bytes()
    value = json.loads(payload)
    if value != protocol_value():
        raise ValueError("效果协议、条件或代码变化，必须另建版本并保留旧结果")
    return value, object_digest(ObjectType.BLOB, encode(value))


class FaultWorker(Worker):
    def __init__(self, queue, case):
        self.case = case
        self.active_faults = None
        self.receipts = {}
        super().__init__(queue, model_factory=self.fixture_model)

    def fixture_model(self):
        assert self.active_faults is not None
        return FixtureModel(self.active_faults)

    def _attach(self, lease):
        self.active_faults = Faults(self.case, True)
        session, origins, remaining = super()._attach(lease)
        self.active_faults.tools(session)
        self.receipts[str(lease.run_id)] = self.active_faults.receipts
        return session, origins, remaining


def capture_bundle(history, proof):
    bundle = pack_repository_evidence(history, proof)
    repo = UUID(proof["repository_id"])
    with history.engine.connect() as connection:
        for name in ("debug_reports", "interventions", "experiments", "experiment_trials", "jobs"):
            table = TABLES[name]
            rows = list(
                connection.execute(sa.select(table).where(table.c.repository_id == repo)).mappings()
            )
            assert len(rows) <= 1000
            bundle["indexes"][name] = [public(dict(row)) for row in rows]
    return bundle


def _execute_case(history, tmp_path, case, progress):
    protocol, identity = load_protocol()
    folder = OUTPUT / "cases" / case.id
    if folder.exists():
        raise FileExistsError("只创建一次的效果证据目录已存在：" + str(folder))
    proof = run_case(history, tmp_path / "source", case, load_dataset())
    progress["source"] = proof
    repo = UUID(proof["repository_id"])
    research = ResearchService(history, tmp_path / "experiment", corpus=Faults(case, True).corpus())
    manager = ManagementService(research)
    validator = ValidatorService(history).freeze(
        repo, ResearchGoal(text="比较三个 fixture 框架"), research.corpus
    )
    condition_ref = history.register_input(
        repo,
        encode(
            dict(
                protocol_ref=identity,
                condition=dict(operation=case.operation, target=case.target),
                enabled_in_both_groups=True,
            )
        ),
    )
    created = manager.create_debug_report(
        repo,
        NewDebugReport(
            failed_run_id=proof["failed_run_id"], reference_run_id=proof["reference_run_id"]
        ),
        "effect-report",
    )
    report_id = UUID(created["debug_report_id"])
    stored = manager.debug_report(repo, report_id)
    static_before = encode(stored)
    source_before = {name: public(history.branch(repo, name)) for name in ("main",)}
    bound, attempts = select_context(manager, repo, report_id, stored, validator)
    result = dict(
        kind="diagnosis-candidate-effect-result/v3",
        case_id=case.id,
        protocol_ref=identity,
        annotation_status="pending_human",
        condition_ref=condition_ref,
        source=proof,
        report=stored,
        context_attempts=attempts,
        effect_status="static_only",
        effect=None,
        source_before=source_before,
        worker_fault_receipts={},
    )
    if bound is not None:
        context = bound["context"]
        snapshot = history.objects.get_json(context["precondition"]["state_ref"], ObjectType.STATE)
        payload = fixed_payload(context, snapshot)
        candidate_event = bound["candidate"]["event"]
        request = NewIntervention.model_validate_json(
            encode(
                {
                    **{
                        key: context[key]
                        for key in (
                            "source_run_id",
                            "base_commit",
                            "target_commit_id",
                            "target_event_id",
                            "precondition",
                        )
                    },
                    "operator": payload["kind"],
                    "payload": payload,
                    "effect_policy": "sandbox_or_approval",
                    "provenance": dict(
                        rationale="固定公开候选效果协议；不读取拟议标签或旧试验结果。",
                        evidence=[
                            dict(run_id=candidate_event["run_id"], seq=candidate_event["seq"])
                        ],
                    ),
                }
            )
        )
        intervention = manager.create_intervention(repo, request, "effect-patch")
        experiment = manager.create_experiment(
            repo,
            NewExperiment(
                source_run_id=context["source_run_id"],
                from_commit=context["base_commit"],
                validator_ref=validator,
                repeats=5,
                budget=BudgetLimits.model_validate(protocol["trial_budget"]),
                groups=(
                    ExperimentGroup(key="A", label="保持故障条件"),
                    ExperimentGroup(
                        key="B",
                        label="同条件单补丁",
                        interventions=(intervention["intervention_id"],),
                    ),
                ),
            ),
            "effect-ab",
        )
        experiment_id = UUID(experiment["experiment_id"])
        pending = manager.debug_candidate_experiment(
            repo, report_id, candidate_event["event_id"], experiment_id
        )
        assert (
            pending["effect_status"] == "incomplete" and pending["success_rate_difference"] is None
        )
        worker = FaultWorker(manager.jobs, case)
        for _ in range(10):
            assert worker.run_once() is not None
        assert worker.run_once() is None
        observed = manager.debug_candidate_experiment(
            repo, report_id, candidate_event["event_id"], experiment_id
        )
        assert len(observed["experiment"]["trials"]) == 10
        assert len({trial["job_id"] for trial in observed["experiment"]["trials"]}) == 10
        assert len({trial["run_id"] for trial in observed["experiment"]["trials"]}) == 10
        if observed["effect_status"] == "incomplete":
            assert observed["success_rate_difference"] is None
        result.update(
            effect_status=observed["effect_status"],
            effect=observed,
            bound=bound,
            patch=request.model_dump(mode="json"),
            worker_fault_receipts=worker.receipts,
        )
    assert encode(manager.debug_report(repo, report_id)) == static_before
    result["source_after"] = {name: public(history.branch(repo, name)) for name in ("main",)}
    assert result["source_before"] == result["source_after"]
    result["static_report_unchanged"] = True
    assert stored["report"]["diagnosis"]["rules_digest"] == protocol["rules_digest"]
    assert stored["report"]["diagnosis"]["intervention_improvement"] is None
    bundle = capture_bundle(history, proof)
    load_protocol()
    folder.mkdir(parents=True, exist_ok=False)
    for name, value in [("result.json", result), ("evidence.json", bundle)]:
        with (folder / name).open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
    print("diagnosis-effect:" + case.id + ":" + result["effect_status"], flush=True)
    return result


def execute_case(history, tmp_path, case):
    progress = {}
    try:
        return _execute_case(history, tmp_path, case, progress)
    except BaseException as error:
        # 运行后驱动错误也保留；已有完整证据的重复执行直接拒绝，不能改写旧案例。
        if progress and not isinstance(error, FileExistsError):
            folder = OUTPUT / "cases" / case.id
            folder.mkdir(parents=True, exist_ok=True)
            failure = dict(
                kind="diagnosis-effect-driver-failure/v1",
                case_id=case.id,
                error_type=type(error).__name__,
                source=progress["source"],
            )
            try:
                bundle = capture_bundle(history, progress["source"])
                with (folder / "failure-evidence.json").open("x", encoding="utf-8") as stream:
                    json.dump(bundle, stream, ensure_ascii=False, indent=2)
            except Exception as archive_error:
                failure["archive_error_type"] = type(archive_error).__name__
            with (folder / "driver-failure.json").open("x", encoding="utf-8") as stream:
                json.dump(failure, stream, ensure_ascii=False, indent=2)
        raise
