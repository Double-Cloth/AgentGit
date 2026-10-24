"""只读关联冻结候选与既有 S7 单变量实验，不修改静态排名或发布新 Job。"""

from fractions import Fraction
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import encode
from agentgit.core.debug_reports import DebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.diagnosis.interventions import candidate_request
from agentgit.services.debug_reports import DebugReportService
from agentgit.services.experiments import ExperimentService
from agentgit.services.history import TABLES


class DebugExperimentService:
    def __init__(self, reports: DebugReportService, experiments: ExperimentService):
        self.reports, self.experiments = reports, experiments
        self.history = reports.history

    def context(
        self, repository: UUID, report_id: UUID, candidate_event_id: int, validator_ref: str
    ) -> dict[str, Any]:
        stored = self.reports.get(repository, report_id)
        report = parse_contract(encode(stored["report"]), DebugReport)
        candidate, request = candidate_request(report, candidate_event_id)
        occurrence = request.occurrence
        assert occurrence is not None and request.event.event_id is not None
        calls = TABLES["external_calls"]
        with self.history.engine.connect() as connection:
            call_id = connection.execute(
                sa.select(calls.c.id).where(
                    calls.c.repository_id == repository,
                    calls.c.run_id == UUID(report.failed.run_id),
                    calls.c.logical_step_id == occurrence.logical_step_id,
                    calls.c.call_index == occurrence.call_index,
                    calls.c.attempt == occurrence.attempt,
                )
            ).scalar_one_or_none()
        if call_id is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "候选调用录制不存在")
        interventions = self.experiments.interventions
        record = interventions.recording(repository, call_id)
        result = next(node for node in report.failed.nodes if node.event == candidate.event)
        if (
            record.intent.occurrence != occurrence
            or record.intent.checkpoint is None
            or record.saved.response_ref != result.observation_ref
            or (record.saved.error["code"] if record.saved.error else None) != result.error_code
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "当前录制与冻结候选调用不同")
        if not record.saved.complete:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "候选录制响应不完整")
        context = interventions.context(
            repository,
            UUID(report.failed.run_id),
            record.intent.checkpoint,
            request.event.event_id,
            validator_ref,
        )
        if (
            context["source_call_id"] != str(call_id)
            or context["target_event_seq"] != request.event.seq
            or context["precondition"]["occurrence"] != occurrence.model_dump(mode="json")
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "当前上下文与冻结候选事件不同")
        return {
            "schema_version": 1,
            "debug_report_id": str(report_id),
            "report_ref": stored["report_ref"],
            "candidate": candidate.model_dump(mode="json"),
            "request_event": request.event.model_dump(mode="json"),
            "context": context,
            "operators": ["PROMPT_PATCH", "PLAN_PATCH"],
            "limitations": [
                "仅核验候选与决策前检查点的关系；干预内容仍须通过 S7 类型及权限检查。",
                "本查询不创建干预或实验，也不证明候选是根因。",
            ],
        }

    def report(
        self, repository: UUID, report_id: UUID, candidate_event_id: int, experiment_id: UUID
    ) -> dict[str, Any]:
        # 先核验报告归属，再读取同项目实验；不接受调用者自报的 trial 或改善率。
        self.reports.get(repository, report_id)
        manifest = self.experiments.manifest(repository, experiment_id)
        request = manifest.request
        bound = self.context(repository, report_id, candidate_event_id, request.validator_ref)
        context = bound["context"]
        baseline, changed = request.groups
        if (
            request.source_run_id != context["source_run_id"]
            or request.from_commit != context["base_commit"]
            or baseline.prompt is not None
            or baseline.plan is not None
            or baseline.interventions
            or changed.prompt is not None
            or changed.plan is not None
            or len(changed.interventions) != 1
        ):
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "实验必须从候选检查点比较未修改 A 与单干预 B"
            )
        intervention_id = UUID(changed.interventions[0])
        definition, payload = self.experiments.interventions.get(repository, intervention_id)
        if (
            definition.source_run_id != context["source_run_id"]
            or definition.source_call_id != context["source_call_id"]
            or definition.base_commit != context["base_commit"]
            or definition.target_commit_id != context["target_commit_id"]
            or definition.target_event_id != context["target_event_id"]
            or definition.target_event_seq != context["target_event_seq"]
            or definition.precondition.model_dump(mode="json") != context["precondition"]
            or definition.operator not in bound["operators"]
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "实验干预没有精确绑定当前候选调用")
        observed = self.experiments.report(repository, experiment_id, persist_assessments=False)
        groups = observed["groups"]
        complete = all(group["incomplete"] == 0 for group in groups)
        improvement = None
        if complete:
            delta = Fraction(groups[1]["passed"], groups[1]["total"]) - Fraction(
                groups[0]["passed"], groups[0]["total"]
            )
            improvement = {"numerator": str(delta.numerator), "denominator": str(delta.denominator)}
        return {
            **bound,
            "kind": "debug_experiment_view/v1",
            "effect_status": "assessed" if complete else "incomplete",
            "success_rate_difference": improvement,
            "intervention_id": str(intervention_id),
            "intervention": definition.model_dump(mode="json"),
            "payload": payload.model_dump(mode="json"),
            "experiment": observed,
            "test_only": True,
            "limitations": [
                *bound["limitations"],
                *observed["limitations"],
                "这是查询时刻的独立实验视图；静态报告及排名不变，未完成时改善率未知。",
                "成功率差是单变量条件比较，不据此宣称严格因果或保留集定位达标。",
            ],
        }
