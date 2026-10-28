"""合并新任务输入不继承轨迹、完成、用量或授权，旧 Job 编码保持可读。"""

from uuid import UUID

import pytest
from pydantic import ValidationError
from test_merge_state import incoming

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError
from agentgit.core.jobs import JobPayload
from agentgit.core.merge_runs import ForkMergedBranch, MergedRunSpec, NewMergedRun
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, BudgetUsage, Plan, PlanNode, evolve
from agentgit.core.snapshots import ArtifactEntry, StateSnapshot
from agentgit.core.versions import LiveReplaySpec
from agentgit.merge.initialize import initialize_merged
from agentgit.merge.state import merge_state
from agentgit.runtime.validation import render_report, report_from_memory


def merged_input():
    items = tuple(incoming(number, usage=BudgetUsage(model_calls=number)) for number in (1, 2, 3))
    candidate = merge_state(*items, Corpus.bundled()).proposed_state
    assert candidate is not None
    report = report_from_memory(items[0].state, Corpus.bundled().evaluation_time)
    files = {
        "report.md": render_report(report).encode(),
        "sources.json": encode(report.model_dump(mode="json")),
    }
    by_ref = {object_digest(ObjectType.BLOB, value): value for value in files.values()}
    snapshot = StateSnapshot(
        data=candidate.model_dump(mode="json"),
        artifacts=tuple(
            ArtifactEntry(
                path=path,
                object_ref=object_digest(ObjectType.BLOB, content),
                media_type="text/markdown" if path == "report.md" else "application/json",
            )
            for path, content in sorted(files.items())
        ),
    )
    return snapshot, lambda ref, kind: by_ref[ref]


def test_initialize_copies_content_and_clears_all_execution_status():
    snapshot, read = merged_input()
    prepared = initialize_merged(snapshot, AgentConfig(), Corpus.bundled(), read)
    state = parse_contract(encode(prepared.data), AgentState)
    assert state.plan is not None and state.plan_version == 1 and state.working_memory
    assert state.completed == state.messages == state.stopped_executions == ()
    assert state.usage == BudgetUsage() and not state.attempts and not state.search_results
    assert state.pending_tool is state.evaluation is state.resume_status is state.error_code is None
    assert prepared.artifacts == snapshot.artifacts
    assert (
        "effects_audit" not in prepared.data
        and snapshot.data["source_usage"][0]["usage"]["model_calls"] == 1
    )


@pytest.mark.parametrize("change", ["limits", "tools", "plan", "files", "clock"])
def test_initialize_rejects_authority_expansion_and_changed_hard_inputs(change):
    snapshot, read = merged_input()
    config, corpus = AgentConfig(), Corpus.bundled()
    if change == "limits":
        config = evolve(config, limits=evolve(config.limits, max_steps=config.limits.max_steps + 1))
    elif change == "tools":
        config = evolve(config, tools=("artifact_write",))
    elif change == "plan":
        config = evolve(config, plan_template=Plan(nodes=(PlanNode(key="other", kind="search"),)))
    elif change == "files":

        def read(ref, kind):
            return b"invalid report"
    else:
        corpus = Corpus(tuple(corpus.sources.values()), evaluation_time="2026-10-29T00:00:00Z")
    with pytest.raises(AgentGitError):
        initialize_merged(snapshot, config, corpus, read)


@pytest.mark.parametrize("name", [" leading", "trailing ", "line\nbreak", ""])
def test_merged_fork_requires_visible_branch_name(name):
    with pytest.raises(ValidationError):
        ForkMergedBranch(approval_id=str(UUID(int=1)), commit_id="a" * 64, name=name)


def payload():
    corpus = Corpus.bundled()
    return JobPayload(
        config_id=str(UUID(int=1)),
        goal_ref="a" * 64,
        code_revision="fixture",
        corpus_digest=corpus.digest(),
        sources=tuple(corpus.sources.values()),
        evaluation_time=corpus.evaluation_time,
        max_wall_ms=60000,
    )


def test_legacy_job_omits_empty_merge_field_and_cannot_mix_two_sources():
    original = payload()
    legacy = original.model_dump(mode="json")
    assert "merged" not in legacy
    assert parse_contract(encode(legacy), JobPayload) == original
    spec = MergedRunSpec(
        approval_id=str(UUID(int=2)),
        from_commit="b" * 64,
        initial_state_ref="c" * 64,
        validator_ref="d" * 64,
    )
    live = LiveReplaySpec(
        source_run_id=str(UUID(int=3)),
        from_commit="b" * 64,
        initial_state_ref="c" * 64,
        environment_ref="e" * 64,
        budget_baseline=BudgetUsage(),
    )
    with pytest.raises(AgentGitError):
        parse_contract(
            encode(
                {
                    **legacy,
                    "merged": spec.model_dump(mode="json"),
                    "replay": live.model_dump(mode="json"),
                }
            ),
            JobPayload,
        )


def test_new_merge_request_cannot_upload_initial_state_or_old_approval():
    data = dict(
        branch_name="work",
        expected_head="a" * 64,
        expected_branch_version=0,
        config_id=str(UUID(int=1)),
        config_version=1,
        validator_ref="b" * 64,
        budget=AgentConfig().limits.model_dump(mode="json"),
    )
    for key in ("initial_state_ref", "source_run_id", "approval", "policy"):
        with pytest.raises(ValidationError):
            NewMergedRun.model_validate_json(encode({**data, key: "untrusted"}))
