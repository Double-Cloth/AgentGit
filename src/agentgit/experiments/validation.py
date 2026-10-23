"""从 CAS 冻结输入重新验收；不采信运行自行上报的 passed。"""

import hashlib
from collections.abc import Mapping
from typing import Literal

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus, ValidatorManifest
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentState, Evaluation, ResearchGoal
from agentgit.runtime.validation import ResearchReport, validate_report
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.storage.objects import ObjectStore


def report_schema_digest() -> str:
    return hashlib.sha256(encode(ResearchReport.model_json_schema())).hexdigest()


def freeze_validator(
    objects: ObjectStore,
    goal: ResearchGoal,
    corpus: Corpus,
    *,
    fixture_split: Literal["screen", "confirm", "regression"],
) -> ValidatorManifest:
    # 编码后重新校验，避免调用方在发布后修改原语料中的可变容器。
    goal = parse_contract(encode(goal.model_dump(mode="json")), ResearchGoal)
    frozen = FrozenCorpus(
        evaluation_time=corpus.evaluation_time,
        sources=tuple(corpus.sources[key] for key in sorted(corpus.sources)),
    )
    captured = Corpus(frozen.sources, evaluation_time=frozen.evaluation_time)
    if set(goal.frameworks) != {source.framework for source in captured.sources.values()}:
        raise AgentGitError(ErrorCode.VALIDATION, "验收输入必须覆盖冻结语料的三个框架")
    return ValidatorManifest(
        input_ref=objects.put_json(ObjectType.BLOB, goal.model_dump(mode="json")).id,
        corpus_ref=objects.put_json(ObjectType.BLOB, frozen.model_dump(mode="json")).id,
        corpus_digest=captured.digest(),
        evaluation_time=frozen.evaluation_time,
        fixture_split=fixture_split,
        code_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        report_schema_digest=report_schema_digest(),
    )


def evaluate_frozen(
    objects: ObjectStore,
    manifest: ValidatorManifest,
    state: AgentState,
    contents: Mapping[str, bytes],
) -> tuple[Literal["passed", "failed", "incomplete"], Evaluation]:
    # 每次加载都核对兼容性，不能让旧 manifest 悄悄使用新代码或新依赖判分。
    manifest = parse_contract(encode(manifest.model_dump(mode="json")), ValidatorManifest)
    if (
        manifest.code_revision != source_revision()
        or manifest.dependency_lock_digest != dependency_lock_digest()
        or manifest.report_schema_digest != report_schema_digest()
    ):
        raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "当前验收代码或依赖与冻结版本不兼容")
    goal = parse_contract(objects.get(manifest.input_ref, ObjectType.BLOB), ResearchGoal)
    frozen = parse_contract(objects.get(manifest.corpus_ref, ObjectType.BLOB), FrozenCorpus)
    corpus = Corpus(frozen.sources, evaluation_time=frozen.evaluation_time)
    if (
        corpus.digest() != manifest.corpus_digest
        or corpus.evaluation_time != manifest.evaluation_time
    ):
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "冻结语料、评估时间或摘要不符")
    if state.goal != goal:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "待验收运行的任务输入与冻结输入不同")
    if state.status not in (RunStatus.COMPLETED, RunStatus.FAILED):
        return "incomplete", Evaluation(passed=False, errors=("run_incomplete",))
    result = validate_report(state, contents, corpus)
    if state.status != RunStatus.COMPLETED:
        result = Evaluation(passed=False, errors=(*result.errors, "run_not_completed"))
    return ("passed" if result.passed else "failed"), result
