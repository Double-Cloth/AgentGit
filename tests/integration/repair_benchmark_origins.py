"""只读公开轨迹选择根/局部合法起点，不接收案例标签或已知注入位置。"""

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import ModelRequest
from agentgit.services.diagnosis import BehaviorService
from agentgit.services.interventions import InterventionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables
ORIGIN_POLICY = "first-act-else-first-plan/nearest-legal-before-first-error/v1"


def select_origins(history, repository, source_run, validator_ref):
    graph = BehaviorService(history).trace(repository, source_run)
    with history.engine.connect() as connection:
        table = TABLES["external_calls"]
        calls = list(
            connection.execute(
                sa.select(table).where(
                    table.c.repository_id == repository,
                    table.c.run_id == source_run,
                )
            ).mappings()
        )
    by_occurrence = {(c["logical_step_id"], c["call_index"], c["attempt"]): c for c in calls}
    candidates, first_error = [], None
    for node in sorted(graph.nodes, key=lambda node: node.event.seq):
        if not node.payload_verified:
            continue
        data = history.objects.get_json(node.payload_ref, ObjectType.EVENT)["data"]
        if first_error is None and (
            node.error_code is not None
            or isinstance(data.get("error"), dict)
            and data["error"].get("code")
            or isinstance(data.get("evaluation"), dict)
            and data["evaluation"].get("passed") is False
        ):
            first_error = node.event
        if node.event_type != "llm.requested" or node.occurrence is None:
            continue
        occurrence = node.occurrence
        row = by_occurrence.get(
            (occurrence.logical_step_id, occurrence.call_index, occurrence.attempt)
        )
        if row is None or not occurrence.provider.startswith("llm:"):
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "公开模型事件缺少所属调用")
        intent = parse_contract(history.objects.get(row["intent_ref"], ObjectType.CALL), CallIntent)
        if (
            intent.occurrence != occurrence
            or intent.checkpoint is None
            or intent.request_ref != occurrence.request_digest
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "公开模型事件与冻结调用不同")
        request = parse_contract(
            history.objects.get(intent.request_ref, ObjectType.BLOB), ModelRequest
        )
        state = request.state
        if state.pending_tool is not None or state.status.value not in ("planning", "running"):
            continue
        if state.plan is not None and not state.plan.ready(state.completed):
            # FinalDecision 之前的空计划不能作为需要新模型决策的独立采样起点。
            continue
        candidates.append(dict(event=node.event, checkpoint=intent.checkpoint, request=request))
    if not candidates:
        raise AgentGitError(ErrorCode.VALIDATION, "来源没有合法的模型前检查点")
    root = next((c for c in candidates if c["request"].phase == "act"), None)
    if root is None:
        root = next((c for c in candidates if c["request"].phase == "plan"), None)
    local = (
        None
        if first_error is None
        else next((c for c in reversed(candidates) if c["event"].seq < first_error.seq), None)
    )
    service = InterventionService(history)

    def context(candidate):
        if candidate is None:
            return None
        selected = service.context(
            repository,
            source_run,
            candidate["checkpoint"],
            candidate["event"].event_id,
            validator_ref,
        )
        if selected["target_commit_id"] != selected["base_commit"]:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "模型模板起点没有直接位于该模型决策之前")
        return dict(
            context=selected,
            event=candidate["event"].model_dump(mode="json"),
            phase=candidate["request"].phase,
            inherited_usage=candidate["request"].state.usage.model_dump(mode="json"),
        )

    return dict(
        policy=ORIGIN_POLICY,
        first_exception=first_error.model_dump(mode="json") if first_error else None,
        root=context(root),
        local=context(local),
    )
