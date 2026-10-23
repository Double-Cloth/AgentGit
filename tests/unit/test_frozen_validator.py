"""S7：冻结规则、真实证据校验及未完成状态不能误通过。"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ValidatorManifest
from agentgit.core.models import RunStatus
from agentgit.core.research import AgentState, Evaluation, Evidence, ResearchGoal, evolve
from agentgit.experiments.validation import evaluate_frozen, freeze_validator
from agentgit.runtime.validation import render_report, report_from_memory
from agentgit.storage.objects import ObjectStore


@pytest.fixture
def frozen_case(tmp_path):
    corpus = Corpus.bundled()
    goal = ResearchGoal(text="比较固定语料中的三个框架")
    objects = ObjectStore(tmp_path / "objects")
    manifest = freeze_validator(objects, goal, corpus, fixture_split="screen")
    state = AgentState(
        goal=goal,
        status=RunStatus.COMPLETED,
        working_memory={
            key: Evidence(source=corpus.read(key), logical_step_id=f"read-{index}", attempt=1)
            for index, key in enumerate(sorted(corpus.sources))
        },
        # 验收必须独立重算，不能沿用运行自己的判断。
        evaluation=Evaluation(passed=False, errors=("untrusted_runtime_result",)),
    )
    report = report_from_memory(state, corpus.evaluation_time)
    contents = {
        "sources.json": encode(report.model_dump(mode="json")),
        "report.md": render_report(report).encode(),
    }
    return objects, manifest, state, contents, corpus


def test_frozen_case_repeats_and_ignores_runtime_evaluation(frozen_case):
    objects, manifest, state, contents, corpus = frozen_case
    again = freeze_validator(objects, state.goal, corpus, fixture_split="screen")
    assert manifest == again
    assert evaluate_frozen(objects, manifest, state, contents) == (
        "passed",
        Evaluation(passed=True),
    )
    assert state.evaluation.errors == ("untrusted_runtime_result",)
    with pytest.raises(ValidationError):
        manifest.fixture_split = "confirm"


@pytest.mark.parametrize("split", ["confirm", "regression"])
def test_split_is_part_of_manifest_identity(frozen_case, split):
    objects, manifest, state, _, corpus = frozen_case
    other = freeze_validator(objects, state.goal, corpus, fixture_split=split)
    first = objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
    second = objects.put_json(ObjectType.BLOB, other.model_dump(mode="json"))
    assert first.id != second.id


@pytest.mark.parametrize(
    "changes",
    [
        {"rules": ()},
        {"rules": ("always_pass",)},
        {"validator": "other-validator/v1"},
        {"fixture_split": "ignore_holdout"},
        {"evaluation_time": "2026-10-20T00:00:00"},
        {"evaluation_time": "2026-10-20T00:00:00+08:00"},
        {"evaluation_time": "not-a-date"},
        {"permissions": ["host_shell"]},
        {"schema_version": True},
    ],
)
def test_manifest_rejects_unreviewed_rules_permissions_and_bad_clock(frozen_case, changes):
    _, manifest, *_ = frozen_case
    with pytest.raises(ValidationError):
        ValidatorManifest.model_validate({**manifest.model_dump(), **changes})


@pytest.mark.parametrize(
    "status",
    [status for status in RunStatus if status not in (RunStatus.COMPLETED, RunStatus.FAILED)],
)
def test_unfinished_or_cancelled_run_with_good_artifacts_is_incomplete(frozen_case, status):
    objects, manifest, state, contents, _ = frozen_case
    verdict, result = evaluate_frozen(
        objects,
        manifest,
        evolve(state, status=status, evaluation=Evaluation(passed=True)),
        contents,
    )
    assert verdict == "incomplete"
    assert not result.passed and result.errors == ("run_incomplete",)


def test_failed_run_with_good_artifacts_cannot_pass(frozen_case):
    objects, manifest, state, contents, _ = frozen_case
    verdict, result = evaluate_frozen(
        objects, manifest, evolve(state, status=RunStatus.FAILED), contents
    )
    assert verdict == "failed"
    assert result.errors == ("run_not_completed",)


@pytest.mark.parametrize("path", ["report.md", "sources.json"])
def test_forged_passed_cannot_hide_bad_artifacts(frozen_case, path):
    objects, manifest, state, contents, _ = frozen_case
    contents[path] = b"forged"
    verdict, result = evaluate_frozen(
        objects, manifest, evolve(state, evaluation=Evaluation(passed=True)), contents
    )
    assert verdict == "failed" and not result.passed


def test_changed_input_is_rejected_even_with_valid_original_report(frozen_case):
    objects, manifest, state, contents, _ = frozen_case
    with pytest.raises(AgentGitError) as caught:
        evaluate_frozen(
            objects, manifest, evolve(state, goal=ResearchGoal(text="更宽松的任务")), contents
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH


@pytest.mark.parametrize(
    "field", ["code_revision", "dependency_lock_digest", "report_schema_digest"]
)
def test_environment_drift_fails_closed(frozen_case, field):
    objects, manifest, state, contents, _ = frozen_case
    value = "source-sha256:" + "0" * 64 if field == "code_revision" else "0" * 64
    with pytest.raises(AgentGitError) as caught:
        evaluate_frozen(objects, evolve(manifest, **{field: value}), state, contents)
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA


def test_original_corpus_mutation_does_not_change_frozen_judgment(frozen_case):
    objects, manifest, state, contents, corpus = frozen_case
    corpus.sources.clear()
    corpus.evaluation_time = "2099-01-01T00:00:00Z"
    assert evaluate_frozen(objects, manifest, state, contents)[0] == "passed"


@pytest.mark.parametrize(
    "field,value", [("corpus_digest", "0" * 64), ("evaluation_time", "2099-01-01T00:00:00Z")]
)
def test_inconsistent_corpus_or_clock_is_corrupt(frozen_case, field, value):
    objects, manifest, state, contents, _ = frozen_case
    with pytest.raises(AgentGitError) as caught:
        evaluate_frozen(objects, evolve(manifest, **{field: value}), state, contents)
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT


def test_cas_corruption_is_not_a_failed_or_successful_trial(frozen_case):
    objects, manifest, state, contents, _ = frozen_case
    objects.path_for(manifest.corpus_ref).write_bytes(b"changed")
    with pytest.raises(AgentGitError) as caught:
        evaluate_frozen(objects, manifest, state, contents)
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT


def test_code_drift_after_freeze_is_detected(frozen_case):
    objects, manifest, state, contents, _ = frozen_case
    with (
        patch("agentgit.experiments.validation.source_revision", return_value="changed"),
        pytest.raises(AgentGitError) as caught,
    ):
        evaluate_frozen(objects, manifest, state, contents)
    assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA
