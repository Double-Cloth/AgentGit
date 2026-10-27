"""完整运行冻结、声明及开发先于保留的门禁；不启动模型或执行搜索。"""

import hashlib
import platform
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
from repair_benchmark_archive import CaseArchive
from repair_benchmark_origins import ORIGIN_POLICY
from repair_benchmark_protocol import METHODS
from repair_benchmark_regressions import regression_inputs
from repair_dataset import DATA, input_ref, load_inputs, task_for_case
from repair_fault_runtime import FaultResearchModel, runtime_input
from repair_fault_specifications import CaseID, load_faults

from agentgit.adapters.mock import MockResearchModel
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.models import Contract, Digest, parse_contract
from agentgit.core.repair import TemplateName
from agentgit.services.environment import dependency_lock_digest, source_revision

ROOT = Path(__file__).resolve().parents[2]
RELEASE_PATH = DATA.parent / "evaluation-release-v1.json"
DECLARATIONS = DATA.parent / "declarations-v1.json"
OUTPUT = ROOT / ".agentgit/evidence/S10-evaluation-v1"
FROZEN = (
    "DESIGN.md",
    "doc/AgentGit_设计文档_v1.0.0.md",
    "doc/AgentGit_实施方案_v1.0.0.md",
    "uv.lock",
    "pyproject.toml",
    "tests/conftest.py",
    "tests/integration/repair_dataset.py",
    "tests/integration/repair_fault_runtime.py",
    "tests/integration/repair_fault_specifications.py",
    "tests/integration/repair_benchmark_release.py",
    "tests/integration/repair_dataset_execution.py",
    "tests/integration/repair_case_execution.py",
    "tests/integration/repair_benchmark_archive.py",
    "tests/integration/repair_benchmark_verify.py",
    "tests/integration/repair_benchmark_environment.py",
    "tests/integration/repair_benchmark_origins.py",
    "tests/integration/repair_benchmark_protocol.py",
    "tests/integration/repair_benchmark_regressions.py",
    "tests/integration/test_repair_frozen_evaluation.py",
    "tests/fixtures/repair/v1/cases.json",
    "tests/fixtures/repair/v1/inputs.json",
    "tests/fixtures/repair/v1/inputs-release.json",
    "tests/fixtures/repair/methods-v1.json",
    "tests/fixtures/repair/regressions-v1.json",
    "tests/fixtures/repair/faults-v2.json",
    "tests/fixtures/repair/declarations-v1.json",
)
FROZEN += ("alembic.ini",) + tuple(
    path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "alembic").rglob("*.py"))
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class CaseDeclaration(Contract):
    case_id: CaseID
    claim: Literal["expected_template_solution", "observation_only", "finite_template_no_solution"]
    solution_templates: Annotated[tuple[TemplateName, ...], Field(max_length=2)]
    allowed_operations: tuple[
        Literal["PROMPT_PATCH"], Literal["PLAN_PATCH"], Literal["TOOL_RESPONSE_PATCH"]
    ]


class Declarations(Contract):
    kind: Literal["repair-case-declarations/v1"] = "repair-case-declarations/v1"
    scope: Literal["evaluation-metadata-only/not-agent-input"]
    status: Literal["declared-before-evaluation/not-observed-scores"]
    cases: Annotated[tuple[CaseDeclaration, ...], Field(min_length=30, max_length=30)]
    no_solution_scope: str
    observation_scope: str

    @model_validator(mode="after")
    def complete_cases(self):
        if tuple(c.case_id for c in self.cases) != tuple(f"R{i:02}" for i in range(1, 31)):
            raise ValueError("逐例声明必须完整且按 ID 固定")
        for item in self.cases:
            if item.claim == "expected_template_solution" and not item.solution_templates:
                raise ValueError("构造解必须列出独立于搜索输入的模板预期")
            if item.claim == "finite_template_no_solution" and item.solution_templates:
                raise ValueError("有限空间无解声明不能同时预填正确补丁")
            if item.claim == "observation_only" and item.solution_templates != (
                "recorded-observation/v1",
            ):
                raise ValueError("观测伪修复不能声明为可采纳配置解")
        return self


class CaseBinding(Contract):
    case_id: CaseID
    split: Literal["train", "heldout"]
    public_input_ref: Digest
    condition_ref: Digest
    fixture_ref: Digest
    source_observation: Literal["canonical", "empty"]


class EvaluationRelease(Contract):
    kind: Literal["frozen-repair-evaluation/v1"] = "frozen-repair-evaluation/v1"
    scope: Literal["offline-mock/30-cases/five-methods"]
    source_revision: str
    dependency_lock_digest: Digest
    python_version: str
    model_interface_signature: Digest
    origin_policy: Literal[ORIGIN_POLICY]
    candidate_policy: Literal["two-fixed-readonly-environments/drop-byte-identical/v1"]
    method_order: tuple[str, ...]
    cases: Annotated[tuple[CaseBinding, ...], Field(min_length=30, max_length=30)]
    regression_input_refs: Annotated[tuple[Digest, ...], Field(min_length=4, max_length=4)]
    frozen_files: dict[str, Digest]
    max_parallel_cases: Literal[4]
    random_seed: Literal[0]
    heldout_gate: Literal["all-twenty-training-cases-complete/v1"]

    def identity(self):
        return object_digest(ObjectType.BLOB, encode(self.model_dump(mode="json")))


def load_declarations():
    declarations = parse_contract(DECLARATIONS.read_bytes(), Declarations)
    for declared, case in zip(declarations.cases, load_faults().cases, strict=True):
        expected = (
            "observation_only"
            if case.category == "observation"
            else (
                "finite_template_no_solution"
                if case.category == "no_solution"
                else "expected_template_solution"
            )
        )
        if declared.claim != expected:
            raise ValueError("逐例解/无解声明与机制类别不同")
    return declarations


def build_release():
    load_declarations()
    specifications, profiles, _ = load_inputs()
    faults = load_faults()
    cases = []
    for original, case in zip(specifications.cases, faults.cases, strict=True):
        public = runtime_input(task_for_case(original, profiles), case.condition)
        cases.append(
            CaseBinding(
                case_id=case.id,
                split=case.split,
                public_input_ref=input_ref(public),
                condition_ref=case.condition.identity(),
                fixture_ref=FaultResearchModel(case.condition).fixture_ref,
                source_observation="empty"
                if case.condition.rank == "empty_fallback"
                else "canonical",
            )
        )
    return EvaluationRelease(
        scope="offline-mock/30-cases/five-methods",
        source_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        python_version=platform.python_version(),
        model_interface_signature=MockResearchModel.signature,
        origin_policy=ORIGIN_POLICY,
        candidate_policy="two-fixed-readonly-environments/drop-byte-identical/v1",
        method_order=METHODS,
        cases=tuple(cases),
        regression_input_refs=tuple(input_ref(t) for t in regression_inputs()),
        frozen_files={name: sha(ROOT / name) for name in FROZEN},
        max_parallel_cases=4,
        random_seed=0,
        heldout_gate="all-twenty-training-cases-complete/v1",
    )


def load_release(path=RELEASE_PATH):
    release = parse_contract(Path(path).read_bytes(), EvaluationRelease)
    if release != build_release():
        raise ValueError("运行/输入/预算/声明/规范已变化，旧 release 不能执行或覆盖；须新版本")
    return release


def training_gate(release, output=OUTPUT):
    for index in range(1, 21):
        archive = CaseArchive(output / "cases" / f"R{index:02}")
        record = archive.manifest["record"]
        if (
            record.get("runtime_release_ref") != release.identity()
            or record.get("case_id") != f"R{index:02}"
            or record.get("scope") != "frozen-evaluation/v1"
            or "driver_failure" in record
            or "active_method" in record
            or set(record.get("methods", {})) != set(METHODS)
        ):
            raise ValueError("所有二十个开发任务必须先完整执行五方法，保留集不可抢跑")
        jobs = archive.manifest["indexes"].get("jobs", [])
        if not jobs or any(
            job["lease_until"] is not None
            or job["status"]
            not in ("completed", "failed", "budget_exhausted", "cancelled", "needs_attention")
            for job in jobs
        ):
            raise ValueError("开发来源及试验仍有活动 Job，不能开始保留集")
        binding = next(case for case in release.cases if case.case_id == record["case_id"])
        if (
            record.get("method_order") != list(METHODS)
            or record.get("public_input_ref") != binding.public_input_ref
            or record.get("condition_ref") != binding.condition_ref
            or record.get("fixture_model_ref") != binding.fixture_ref
            or record.get("source_before") != record.get("source_after")
        ):
            raise ValueError("开发记录与冻结输入、顺序或来源不符")
        searches = {
            row["search_id"]: row["status"]
            for row in archive.manifest["indexes"].get("repair_jobs", [])
        }
        studies = {
            row["study_id"]: row["status"]
            for row in archive.manifest["indexes"].get("repair_study_jobs", [])
        }
        for outcome in record["methods"].values():
            status = (
                studies.get(outcome["study_id"])
                if outcome.get("study_id")
                else searches.get(outcome.get("search_id"))
            )
            if status != outcome.get("status") or status not in (
                "verified_under_fixture",
                "rejected",
                "needs_review",
                "budget_exhausted",
                "no_solution_in_space",
                "not_found",
                "needs_attention",
                "cancelled",
                "awaiting_samples",
            ):
                raise ValueError("开发方法记录没有对应的真实停止状态")


def case_permission(case_id, folder):
    release = load_release()
    binding = next(case for case in release.cases if case.case_id == case_id)
    if Path(folder).resolve() != (OUTPUT / "cases" / case_id).resolve():
        raise ValueError("冻结评估只能写入当前 release 的固定独立证据目录")
    if binding.split == "heldout":
        training_gate(release)
    return release, binding
