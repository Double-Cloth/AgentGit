"""正式故障规格的版本边界；原输入资产仍保留，不覆盖未验证的旧家族标签。"""

import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator
from repair_dataset import CASE_COUNTS, DATA, Category, Key, Split, load_inputs
from repair_fault_runtime import FaultCondition

from agentgit.core.canonical import encode
from agentgit.core.models import Contract, Digest, parse_contract

FAULTS = DATA.parent / "faults-v2.json"
CaseID = Annotated[str, Field(pattern=r"^R[0-9]{2}$")]


class RuntimeFamily(Contract):
    key: Key
    split: Split
    members: Annotated[tuple[CaseID, ...], Field(min_length=1, max_length=30)]


class RuntimeCase(Contract):
    id: CaseID
    split: Split
    category: Category
    declared_repairable: bool
    family: Key
    pattern: Key
    condition: FaultCondition


class FaultSpecifications(Contract):
    kind: Literal["repair-fault-specifications/v2"] = "repair-fault-specifications/v2"
    stage: Literal["runtime-specifications-draft"] = "runtime-specifications-draft"
    label_origin: Literal["agent_proposed"] = "agent_proposed"
    label_status: Literal["unverified"] = "unverified"
    heldout_execution: Literal["not_started"] = "not_started"
    input_release_sha256: Digest
    change_reason: str
    families: Annotated[tuple[RuntimeFamily, ...], Field(min_length=11, max_length=11)]
    cases: Annotated[tuple[RuntimeCase, ...], Field(min_length=30, max_length=30)]

    @model_validator(mode="after")
    def partition_and_mechanisms(self):
        if tuple(c.id for c in self.cases) != tuple(f"R{i:02}" for i in range(1, 31)):
            raise ValueError("运行规格必须完整且依次包含 R01–R30")
        families = {f.key: f for f in self.families}
        if len(families) != 11 or set(families) != {c.family for c in self.cases}:
            raise ValueError("家族必须唯一且全部使用")
        for family in self.families:
            members = tuple(c.id for c in self.cases if c.family == family.key)
            if members != family.members or any(
                c.split != family.split for c in self.cases if c.id in members
            ):
                raise ValueError("同一机制家族不能跨划分或重复归属")
        if len({encode(c.condition.model_dump(mode="json")) for c in self.cases}) != 30:
            raise ValueError("同一故障条件不能仅改元数据重用")
        for category, (train, heldout) in CASE_COUNTS.items():
            if tuple(
                sum(c.category == category and c.split == split for c in self.cases)
                for split in ("train", "heldout")
            ) != (train, heldout):
                raise ValueError("五类任务配额不能按结果改变")
        if {c.id for c in self.cases if c.split == "train" and c.declared_repairable} != {
            f"R{i:02}" for i in range(1, 13)
        }:
            raise ValueError("十二个声明可修复开发任务的分母固定")
        instructions = [c for c in self.cases if c.condition.instruction != "none"]
        if {c.id for c in instructions} != {"R23", "R24", "R25", "R26", "R29", "R30"} or any(
            c.split != "heldout" or c.family != "untrusted-source-instruction" for c in instructions
        ):
            raise ValueError("同源外部指令模板必须完整留在保留家族")
        unsafe = [c for c in self.cases if c.condition.unsafe_path != "none"]
        if {c.id for c in unsafe} != {"R19", "R20"} or any(
            c.family != "artifact-path-normalization" or c.condition.instruction != "none"
            for c in unsafe
        ):
            raise ValueError("开发路径缺陷不能复用保留指令模板")
        return self


def load_faults():
    specifications = parse_contract(FAULTS.read_bytes(), FaultSpecifications)
    base, _, _ = load_inputs()
    if (
        hashlib.sha256((DATA / "inputs-release.json").read_bytes()).hexdigest()
        != specifications.input_release_sha256
    ):
        raise ValueError("故障规格没有绑定原输入 release")
    for original, case in zip(base.cases, specifications.cases, strict=True):
        if (original.id, original.split, original.category, original.declared_repairable) != (
            case.id,
            case.split,
            case.category,
            case.declared_repairable,
        ):
            raise ValueError("运行规格不能静默换任务、划分、类别或修复分母")
    return specifications
