"""Live 干预仅改变副本，并把执行参数绑定到新文件清单和策略。"""

import pytest

from agentgit.adapters.execution.policy import bundled_policy
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import (
    AgentConfig,
    AgentState,
    BudgetLimits,
    BudgetUsage,
    Plan,
    PlanNode,
    ResearchGoal,
    ToolDecision,
    evolve,
)
from agentgit.core.snapshots import ArtifactEntry, StateSnapshot
from agentgit.core.versions import ArtifactPatch, ExecutionOverride, LiveOverrides, parse_replay
from agentgit.replay.live import prepare_live
from agentgit.replay.workspace import MemoryWorkspace
from agentgit.runtime.engine import ResearchRuntime


def source(*, execution=False):
    config = (
        AgentConfig(tools=(*AgentConfig().tools, "code_exec"), execution_policy=bundled_policy())
        if execution
        else AgentConfig()
    )
    plan = Plan(
        nodes=(
            PlanNode(key="first", kind="search"),
            PlanNode(
                key="next",
                kind="code_exec" if execution else "read",
                dependencies=("first",),
                **({} if execution else {"source_slot": 0}),
            ),
        )
    )
    state = AgentState(
        goal=ResearchGoal(text="比较三个合成框架"),
        status=RunStatus.RUNNING,
        plan=plan,
        plan_version=1,
        completed=("first",),
        attempts={"first": 1, "next": 2},
        usage=BudgetUsage(steps=50, model_calls=50, tool_calls=40, tokens=2_000_000, replans=2),
        pending_tool=ToolDecision(
            node_key="next",
            tool="code_exec" if execution else "source_read",
            arguments={
                "script_path": "main.py",
                "input_manifest_ref": "a" * 64,
                "execution_policy_ref": config.execution_policy.digest(),
            }
            if execution
            else {"source_id": "fixture"},
        ),
        error_code="tool_error",
        repeated_actions=2,
        last_action="old",
    )
    workspace = MemoryWorkspace(max_file_bytes=65536, max_files=100)
    workspace.replace(
        {"main.py": b"print('old')", "old.bin": b"\x00\xff"},
        {"main.py": "text/x-python", "old.bin": "application/octet-stream"},
    )
    snapshot = workspace.snapshot(state)
    blobs = {
        object_digest(ObjectType.BLOB, value): value for value in workspace.contents().values()
    }
    return snapshot, config, lambda ref, kind: blobs[ref]


def test_live_file_patch_keeps_source_and_resets_only_pending_attempts():
    snapshot, config, read = source()
    before = encode(snapshot.model_dump(mode="json"))
    prepared = prepare_live(
        snapshot,
        config,
        LiveOverrides(
            artifacts=(
                ArtifactPatch(path="main.py", text="print('new')"),
                ArtifactPatch(path="old.bin", operation="delete"),
                ArtifactPatch(
                    path="new.bin", content_b64="AP8=", media_type="application/octet-stream"
                ),
            )
        ),
        read,
    )
    state = parse_contract(encode(prepared.snapshot.data), AgentState)
    assert prepared.files == {"main.py": b"print('new')", "new.bin": b"\x00\xff"}
    assert state.attempts == {"first": 1} and state.completed == ("first",)
    assert state.usage == prepared.budget_baseline
    assert state.error_code is None and state.last_action is None and state.repeated_actions == 0
    assert encode(snapshot.model_dump(mode="json")) == before


@pytest.mark.parametrize(
    "patch",
    [
        {"path": "../escape", "text": "x"},
        {"path": "file"},
        {"path": "file", "text": "x", "content_b64": "eA=="},
        {"path": "file", "content_b64": "not base64"},
        {"path": "file", "operation": "delete", "text": "x"},
    ],
)
def test_live_rejects_ambiguous_or_unsafe_file_inputs(patch):
    with pytest.raises(AgentGitError) as error:
        parse_contract(encode({"schema_version": 1, **patch}), ArtifactPatch)
    assert error.value.code == ErrorCode.VALIDATION


def test_live_rejects_case_conflicting_paths_and_recorded_mode_interventions():
    for mode, changes in (
        (
            "live",
            {
                "overrides": {
                    "artifacts": [
                        {"path": "File", "text": "a"},
                        {"path": "file", "text": "b"},
                    ]
                }
            },
        ),
        ("recorded", {"until_commit": "b" * 64, "overrides": {}}),
        ("live", {"until_commit": "b" * 64}),
    ):
        with pytest.raises(AgentGitError):
            parse_replay(
                encode(
                    {
                        "schema_version": 1,
                        "mode": mode,
                        "source_run_id": "00000000-0000-0000-0000-000000000001",
                        "from_commit": "a" * 64,
                        "branch_name": "live",
                        **changes,
                    }
                )
            )


def test_live_cannot_rewrite_completed_node_or_remove_required_permission():
    snapshot, config, read = source()
    with pytest.raises(AgentGitError) as error:
        prepare_live(
            snapshot,
            config,
            LiveOverrides(plan=Plan(nodes=(PlanNode(key="first", kind="write_report"),))),
            read,
        )
    assert error.value.code == ErrorCode.PLAN_INVALID
    with pytest.raises(AgentGitError) as error:
        prepare_live(snapshot, evolve(config, tools=("corpus_search",)), LiveOverrides(), read)
    assert error.value.code == ErrorCode.FORBIDDEN


def test_live_code_override_rebinds_new_manifest_and_accepts_versioned_stdin():
    snapshot, config, read = source(execution=True)
    prepared = prepare_live(
        snapshot,
        config,
        LiveOverrides(
            artifacts=(
                ArtifactPatch(path="main.py", text="print(input())"),
                ArtifactPatch(path="input.txt", text="fresh"),
            ),
            execution=ExecutionOverride(
                tool="code_exec",
                path="main.py",
                argv=("a",),
                stdin_path="input.txt",
                timeout_ms=1000,
            ),
        ),
        read,
    )
    pending = prepared.snapshot.data["pending_tool"]["arguments"]
    assert pending["script_path"] == "main.py" and pending["argv"] == ["a"]
    assert pending["input_manifest_ref"] != "a" * 64
    assert pending["execution_policy_ref"] == config.execution_policy.digest()
    assert pending["stdin_ref"] == object_digest(ObjectType.BLOB, b"fresh")
    assert pending["timeout_ms"] == 1000


def test_live_execution_rejects_missing_script_and_ambiguous_ready_node():
    snapshot, config, read = source(execution=True)
    for override in (
        ExecutionOverride(tool="code_exec", path="absent.py"),
        ExecutionOverride(tool="code_exec", path="main.py", node_key="completed"),
    ):
        with pytest.raises(AgentGitError):
            prepare_live(snapshot, config, LiveOverrides(execution=override), read)


def test_budget_baseline_cannot_claim_usage_not_in_state():
    snapshot, config, _ = source()
    state = parse_contract(encode(snapshot.data), AgentState)
    with pytest.raises(AgentGitError) as error:
        ResearchRuntime(
            state,
            config,
            None,
            None,
            None,
            None,
            budget_baseline=evolve(state.usage, tool_calls=state.usage.tool_calls + 1),
        )
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_live_source_loading_stops_at_aggregate_bound_before_reading_all_files():
    payload = b"x" * 1_048_576
    reference = object_digest(ObjectType.BLOB, payload)
    snapshot = StateSnapshot(
        data=AgentState(goal=ResearchGoal(text="有界来源")).model_dump(mode="json"),
        artifacts=tuple(
            ArtifactEntry(
                path=f"file-{index}", object_ref=reference, media_type="application/octet-stream"
            )
            for index in range(70)
        ),
    )
    calls = []

    def read(ref, kind):
        calls.append(ref)
        return payload

    with pytest.raises(AgentGitError) as error:
        prepare_live(
            snapshot,
            AgentConfig(limits=BudgetLimits(max_artifact_bytes=1_048_576)),
            LiveOverrides(),
            read,
        )
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
    assert len(calls) == 65


def test_live_without_execution_uses_workspace_bound_instead_of_container_manifest_bound():
    payload = b"x" * 1_048_576
    snapshot = StateSnapshot(
        data=AgentState(goal=ResearchGoal(text="纯产物工作区")).model_dump(mode="json"),
        artifacts=tuple(
            ArtifactEntry(
                path=f"file-{index}",
                object_ref=object_digest(ObjectType.BLOB, payload),
                media_type="application/octet-stream",
            )
            for index in range(34)
        ),
    )
    prepared = prepare_live(
        snapshot,
        AgentConfig(limits=BudgetLimits(max_artifact_bytes=1_048_576)),
        LiveOverrides(),
        lambda ref, kind: payload,
    )
    assert len(prepared.files) == len(prepared.snapshot.artifacts) == 34
