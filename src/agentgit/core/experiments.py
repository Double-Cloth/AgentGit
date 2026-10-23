"""S7 冻结验收器的内容契约，不接受模型定义的判分规则。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.models import Contract, Digest, Identifier
from agentgit.core.research import BudgetLimits, Evaluation, Plan, Source

RESEARCH_RULES = (
    "required_artifacts",
    "fixture_and_evaluation_time",
    "framework_coverage",
    "source_coverage",
    "evidence_identity_and_provenance",
    "source_validity",
    "claim_coverage_and_quotes",
    "markdown_consistency",
)


class FrozenCorpus(Contract):
    evaluation_time: str
    sources: Annotated[tuple[Source, ...], Field(min_length=1, max_length=1000)]


class ValidatorManifest(Contract):
    kind: Literal["frozen_research_validator/v1"] = "frozen_research_validator/v1"
    validator: Literal["research-validator/v1"] = "research-validator/v1"
    rules: tuple[str, ...] = RESEARCH_RULES
    input_ref: Digest
    corpus_ref: Digest
    corpus_digest: Digest
    evaluation_time: str
    fixture_id: Literal["research-corpus/v1"] = "research-corpus/v1"
    fixture_split: Literal["screen", "confirm", "regression"]
    code_revision: Annotated[str, Field(pattern=r"^source-sha256:[0-9a-f]{64}$")]
    dependency_lock_digest: Digest
    report_schema_digest: Digest

    @model_validator(mode="after")
    def fixed_rules_and_clock(self) -> "ValidatorManifest":
        if self.rules != RESEARCH_RULES:
            raise ValueError("验收规则必须使用已审阅的固定集合及顺序")
        clock = datetime.fromisoformat(self.evaluation_time.replace("Z", "+00:00"))
        offset = clock.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("冻结评估时间必须为有效 UTC 时间")
        return self


class FrozenAssessment(Contract):
    validator_ref: Digest
    run_id: Identifier
    commit_id: Digest
    state_ref: Digest
    verdict: Literal["passed", "failed", "incomplete"]
    evaluation: Evaluation
    test_only: Literal[True] = True

    @model_validator(mode="after")
    def consistent_verdict(self) -> "FrozenAssessment":
        if (self.verdict == "passed") != self.evaluation.passed:
            raise ValueError("未完成或失败的运行不得通过实验验收")
        return self


class ExperimentGroup(Contract):
    key: Literal["A", "B"]
    label: Annotated[str, Field(min_length=1, max_length=200)]
    prompt: Annotated[str | None, Field(min_length=1, max_length=16000)] = None
    plan: Plan | None = None


class FreezeValidator(Contract):
    source_run_id: Identifier
    from_commit: Digest
    fixture_split: Literal["screen", "confirm", "regression"] = "screen"


class NewExperiment(Contract):
    source_run_id: Identifier
    from_commit: Digest
    validator_ref: Digest
    groups: Annotated[tuple[ExperimentGroup, ...], Field(min_length=2, max_length=2)]
    repeats: Annotated[int, Field(ge=1, le=10)] = 5
    budget: BudgetLimits

    @model_validator(mode="after")
    def two_ordered_groups(self) -> "NewExperiment":
        if tuple(group.key for group in self.groups) != ("A", "B"):
            raise ValueError("实验必须按 A、B 顺序声明两个不同分组")
        return self


class ExperimentManifest(Contract):
    kind: Literal["branch_experiment/v1"] = "branch_experiment/v1"
    mode: Literal["live"] = "live"
    request: NewExperiment
    source_environment_ref: Digest
    source_state_ref: Digest
    source_config_ref: Digest
    random_seed: Literal[0] = 0
    test_only: Literal[True] = True
