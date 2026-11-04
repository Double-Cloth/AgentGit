"""新的修复任务输入与评估元数据隔离；本模块不注入故障或计算研究成绩。"""

import hashlib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.adapters.corpus import Corpus, utc_time
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, Digest, parse_contract
from agentgit.core.research import DEFAULT_FRAMEWORKS, ResearchGoal, Source, SourceFact
from agentgit.runtime.validation import DIMENSIONS

DATA = Path(__file__).resolve().parents[1] / "fixtures/repair/v1"
Split = Literal["train", "heldout"]
Category = Literal["single", "joint", "observation", "no_solution", "side_effect"]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{2,39}$")]
CASE_COUNTS = {
    "single": (6, 4),
    "joint": (6, 2),
    "observation": (3, 1),
    "no_solution": (3, 1),
    "side_effect": (2, 2),
}


class InputProfile(Contract):
    key: Key
    title: Annotated[str, Field(min_length=1, max_length=100)]
    evaluation_time: str
    values: tuple[tuple[str, str, str], tuple[str, str, str], tuple[str, str, str]]


class Profiles(Contract):
    kind: Literal["repair-public-profiles/v1"] = "repair-public-profiles/v1"
    profiles: Annotated[tuple[InputProfile, ...], Field(min_length=30, max_length=30)]

    @model_validator(mode="after")
    def unique_profiles(self):
        if len({p.key for p in self.profiles}) != 30:
            raise ValueError("公开任务 profile 必须恰好三十个且不重复")
        return self


class TaskInput(Contract):
    kind: Literal["repair-public-input/v1"] = "repair-public-input/v1"
    goal: ResearchGoal
    evaluation_time: str
    sources: Annotated[tuple[Source, ...], Field(min_length=6, max_length=6)]


class FaultFamily(Contract):
    key: Key
    split: Split
    mechanism: Annotated[str, Field(min_length=20, max_length=1000)]
    patterns: Annotated[tuple[Key, ...], Field(min_length=1, max_length=8)]


class RepairCase(Contract):
    id: Annotated[str, Field(pattern=r"^R[0-9]{2}$")]
    split: Split
    family: Key
    category: Category
    pattern: Key
    profile: Key
    public_input_ref: Digest
    declared_repairable: bool


class InputSpecifications(Contract):
    kind: Literal["repair-input-specifications/v1"] = "repair-input-specifications/v1"
    stage: Literal["input-specifications-only"] = "input-specifications-only"
    label_origin: Literal["agent_proposed"] = "agent_proposed"
    label_status: Literal["unverified"] = "unverified"
    source_rule: Literal["first-act-else-first-plan/v1"] = "first-act-else-first-plan/v1"
    families: Annotated[tuple[FaultFamily, ...], Field(min_length=2, max_length=30)]
    cases: Annotated[tuple[RepairCase, ...], Field(min_length=30, max_length=30)]

    @model_validator(mode="after")
    def partition(self):
        if tuple(case.id for case in self.cases) != tuple(f"R{i:02}" for i in range(1, 31)):
            raise ValueError("任务必须完整且依次包含 R01–R30，不漏例或重复")
        if (
            len({case.profile for case in self.cases}) != 30
            or len({case.public_input_ref for case in self.cases}) != 30
        ):
            raise ValueError("三十项任务必须绑定不同公开输入")
        families = {family.key: family for family in self.families}
        if len(families) != len(self.families) or set(families) != {
            case.family for case in self.cases
        }:
            raise ValueError("家族必须唯一且逐项使用，不接受未知或闲置家族")
        patterns = [pattern for family in self.families for pattern in family.patterns]
        if len(patterns) != len(set(patterns)):
            raise ValueError("同一故障模板不能改家族名跨划分")
        for case in self.cases:
            family = families[case.family]
            if family.split != case.split or case.pattern not in family.patterns:
                raise ValueError("任务划分必须按完整故障家族，模板须属于声明家族")
        for category, (train, heldout) in CASE_COUNTS.items():
            actual = tuple(
                sum(case.category == category and case.split == split for case in self.cases)
                for split in ("train", "heldout")
            )
            if actual != (train, heldout):
                raise ValueError("五类任务的 20/10 配额不符")
        if {
            case.id for case in self.cases if case.split == "train" and case.declared_repairable
        } != {f"R{i:02}" for i in range(1, 13)}:
            raise ValueError("开发集十二项拟议可修复任务在执行前固定，不能按结果改分母")
        return self


class InputRelease(Contract):
    kind: Literal["repair-input-assets-release/v1"] = "repair-input-assets-release/v1"
    scope: Literal["public-inputs-only/not-fault-runtime-or-evaluation"]
    cases_sha256: Digest
    profiles_sha256: Digest
    builder_sha256: Digest
    methods_sha256: Digest
    train: Annotated[tuple[str, ...], Field(min_length=20, max_length=20)]
    heldout: Annotated[tuple[str, ...], Field(min_length=10, max_length=10)]
    fault_runtime_ref: None = None
    heldout_evaluation: Literal["not_started"] = "not_started"


def public_input(profile: InputProfile) -> TaskInput:
    sources = []
    for framework, values in zip(DEFAULT_FRAMEWORKS, profile.values, strict=True):
        for section, indices in (("overview", (0, 1)), ("scope", (2,))):
            facts = tuple(
                SourceFact(
                    dimension=DIMENSIONS[i],
                    value=values[i],
                    quote=f"{framework} 的{DIMENSIONS[i]}为{values[i]}。",
                )
                for i in indices
            )
            body = (
                f"本资料为固定合成 fixture，不描述真实产品。研究主题：{profile.title}。\n"
                + "\n".join(fact.quote for fact in facts)
            )
            sources.append(
                Source(
                    id=f"{framework.lower()}-{profile.key}-{section}",
                    framework=framework,
                    url=f"https://{framework.lower()}.research.invalid/{profile.key}/{section}",
                    collected_at="2026-01-01T00:00:00Z",
                    valid_from="2025-01-01T00:00:00Z",
                    valid_until="2028-01-01T00:00:00Z",
                    body=body,
                    body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                    facts=facts,
                )
            )
    corpus = Corpus(tuple(sources), evaluation_time=profile.evaluation_time)
    clock = utc_time(profile.evaluation_time)
    if any(
        not (utc_time(s.valid_from) <= clock < utc_time(s.valid_until))
        or utc_time(s.collected_at) > clock
        for s in sources
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "公开基准输入必须在固定验收时间有效")
    catalog = ", ".join(sorted(corpus.sources))
    goal = ResearchGoal(
        text=f"比较{profile.title}中的三个合成框架，核对调度方式、状态存储和适用规模。"
        "仅使用登记资料，生成相互一致的 report.md 与 sources.json。"
        f"资料目录：{catalog}。"
    )
    return TaskInput(goal=goal, evaluation_time=profile.evaluation_time, sources=tuple(sources))


def input_ref(value: TaskInput) -> str:
    return object_digest(ObjectType.BLOB, encode(value.model_dump(mode="json")))


def task_for_case(case: RepairCase, profiles: Profiles) -> TaskInput:
    matches = [p for p in profiles.profiles if p.key == case.profile]
    if len(matches) != 1:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "任务没有唯一公开 profile")
    value = public_input(matches[0])
    if input_ref(value) != case.public_input_ref:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "任务公开输入与摘要不同")
    return value


def verify_inputs(specifications, profiles):
    by_key = {p.key: p for p in profiles.profiles}
    if {case.profile for case in specifications.cases} != set(by_key):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "公开 profile 集合与任务不一致")
    for case in specifications.cases:
        task_for_case(case, profiles)


def load_inputs():
    cases = (DATA / "cases.json").read_bytes()
    profiles_payload = (DATA / "inputs.json").read_bytes()
    specifications = parse_contract(cases, InputSpecifications)
    profiles = parse_contract(profiles_payload, Profiles)
    release = parse_contract((DATA / "inputs-release.json").read_bytes(), InputRelease)
    expected = (
        release.cases_sha256,
        release.profiles_sha256,
        release.builder_sha256,
        release.methods_sha256,
    )
    actual = tuple(
        hashlib.sha256(value).hexdigest()
        for value in (
            cases,
            profiles_payload,
            Path(__file__).read_bytes(),
            (DATA.parent / "methods-v1.json").read_bytes(),
        )
    )
    if (
        expected != actual
        or release.train != tuple(c.id for c in specifications.cases if c.split == "train")
        or release.heldout != tuple(c.id for c in specifications.cases if c.split == "heldout")
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "输入资产/划分摘要已改变，须建立新版本")
    verify_inputs(specifications, profiles)
    return specifications, profiles, release
