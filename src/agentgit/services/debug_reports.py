"""只从已核验终态提取失败断言，冻结静态候选报告；不执行任何干预。"""

from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.debug_reports import (
    MAX_ASSERTIONS,
    DebugReport,
    FailureAssertion,
    NewDebugReport,
)
from agentgit.core.diagnosis import BehavioralDiff, TraceGraph
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CommitManifest, EnvironmentManifest, RunStatus, parse_contract
from agentgit.core.research import AgentState
from agentgit.core.snapshots import EventPayload, StateSnapshot
from agentgit.diagnosis.candidates import static_diagnosis
from agentgit.services.diagnosis import MAX_REPORT_BYTES, BehaviorService
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, HistoryService


class DebugReportService:
    def __init__(self, history: HistoryService):
        self.history = history
        self.behavior = BehaviorService(history)

    def _owned(
        self, repository: UUID, owner_type: str, owner: str, role: str, ref: str, kind: ObjectType
    ) -> bytes:
        table = TABLES["object_refs"]
        with self.history.engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.object_id).where(
                    table.c.repository_id == repository,
                    table.c.owner_type == owner_type,
                    table.c.owner_id == owner,
                    table.c.role == role,
                    table.c.object_id == ref,
                )
            ).first()
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "定位报告证据不属于当前实体验证范围")
        return self.history.objects.get(ref, kind)

    def _head(
        self, repository: UUID, graph: TraceGraph
    ) -> tuple[CommitManifest, StateSnapshot, EnvironmentManifest] | None:
        head = graph.head_commit
        if head is None:
            return None
        manifest = parse_contract(
            self._owned(repository, "commit", head, "manifest", head, ObjectType.COMMIT),
            CommitManifest,
        )
        state = parse_contract(
            self._owned(repository, "commit", head, "state", manifest.state_ref, ObjectType.STATE),
            StateSnapshot,
        )
        environment = parse_contract(
            self._owned(
                repository,
                "commit",
                head,
                "environment",
                manifest.environment_ref,
                ObjectType.ENVIRONMENT,
            ),
            EnvironmentManifest,
        )
        return manifest, state, environment

    def _assertions(self, repository: UUID, graph: TraceGraph) -> tuple[FailureAssertion, ...]:
        if graph.run_status not in {"failed", "budget_exhausted"}:
            return ()
        head = self._head(repository, graph)
        if head is None:
            return ()
        manifest, snapshot, _ = head
        if "goal" not in snapshot.data or "plan_version" not in snapshot.data:
            return ()
        state = parse_contract(encode(snapshot.data), AgentState)
        if state.status.value != graph.run_status:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "失败 Run 与捕获终态不符")
        seq = manifest.event_range.last_seq
        table = TABLES["snapshots"]
        with self.history.engine.connect() as connection:
            own_snapshot = connection.execute(
                sa.select(table.c.event_seq).where(
                    table.c.repository_id == repository,
                    table.c.run_id == UUID(graph.run_id),
                    table.c.commit_id == graph.head_commit,
                )
            ).scalar_one_or_none()
        node = next((node for node in graph.nodes if node.event.seq == seq), None)
        if (
            own_snapshot != seq
            or seq != graph.as_of_seq
            or node is None
            or not node.payload_verified
            or any(gap.event == node.event for gap in graph.gaps)
        ):
            return ()
        data = parse_contract(
            self._owned(
                repository, "event", node.event.key, "payload", node.payload_ref, ObjectType.EVENT
            ),
            EventPayload,
        ).data
        if (
            node.event_type == "run.evaluated"
            and state.status == RunStatus.FAILED
            and state.evaluation is not None
            and not state.evaluation.passed
        ):
            if data.get("evaluation") != state.evaluation.model_dump(mode="json"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "失败硬断言与终态验收记录不符")
            codes = tuple(dict.fromkeys(state.evaluation.errors)) or ("validation_failed",)
            if len(codes) > MAX_ASSERTIONS or any(len(code) > 200 for code in codes):
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "失败断言超过定位内容上限")
            return tuple(
                FailureAssertion(event=node.event, origin="research_validator", code=code)
                for code in codes
            )
        if node.event_type == "run.stopped" and state.error_code is not None:
            error = data.get("error")
            if not isinstance(error, dict) or error.get("code") != state.error_code:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Runtime 停止原因与终态不符")
            return (
                FailureAssertion(event=node.event, origin="runtime_stop", code=state.error_code),
            )
        return ()

    def compile(self, repository: UUID, request: NewDebugReport) -> DebugReport:
        failed, reference = (
            UUID(request.failed_run_id),
            UUID(request.reference_run_id) if request.reference_run_id else None,
        )
        comparison: BehavioralDiff | None = None
        right: TraceGraph | None = None
        if reference is None:
            left = self.behavior.trace(repository, failed)
        else:
            raw = self.behavior.compare(repository, failed, reference)
            left = parse_contract(encode(raw["left"]), TraceGraph)
            right = parse_contract(encode(raw["right"]), TraceGraph)
            comparison = parse_contract(encode(raw["comparison"]), BehavioralDiff)
            successful = self._head(repository, right)
            if right.run_status != "completed" or not right.scope_complete or successful is None:
                raise AgentGitError(ErrorCode.VALIDATION, "参考 Run 必须是具有完整检查点的成功运行")
            state = parse_contract(encode(successful[1].data), AgentState)
            if (
                state.status != RunStatus.COMPLETED
                or state.evaluation is None
                or not state.evaluation.passed
            ):
                raise AgentGitError(ErrorCode.VALIDATION, "参考 Run 缺少通过的硬验收终态")
            terminal_seq = successful[0].event_range.last_seq
            terminal = next((node for node in right.nodes if node.event.seq == terminal_seq), None)
            if (
                terminal_seq != right.as_of_seq
                or terminal is None
                or not terminal.payload_verified
                or terminal.event_type != "run.evaluated"
            ):
                raise AgentGitError(ErrorCode.VALIDATION, "参考 Run 缺少已提交的成功验收事件")
            evaluation = parse_contract(
                self._owned(
                    repository,
                    "event",
                    terminal.event.key,
                    "payload",
                    terminal.payload_ref,
                    ObjectType.EVENT,
                ),
                EventPayload,
            ).data.get("evaluation")
            if evaluation != state.evaluation.model_dump(mode="json"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "参考 Run 的验收事件与终态不符")
        if left.run_status == "completed":
            raise AgentGitError(ErrorCode.VALIDATION, "成功运行不能冒充失败输入")
        assertions = self._assertions(repository, left)
        changes: tuple[str, ...] = ()
        a = self._head(repository, left)
        b = self._head(repository, right) if right else None
        if a is not None and b is not None:
            changes = tuple(
                name
                for name in type(a[2]).model_fields
                if name != "schema_version" and getattr(a[2], name) != getattr(b[2], name)
            )
        report = DebugReport(
            repository_id=str(repository),
            failed=left,
            reference=right,
            comparison=comparison,
            environment_changes=changes,
            diagnosis=static_diagnosis(left, assertions, comparison),
            source_revision=source_revision(),
        )
        if len(encode(report.model_dump(mode="json"))) > MAX_REPORT_BYTES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "定位报告超过输出上限，不能截断候选或证据")
        return report

    def create(
        self, repository: UUID, request: NewDebugReport, actor: str, connection: Connection
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "定位报告必须记录有效操作者")
        report = self.compile(repository, request)
        identity = uuid4()
        blob = self.history.objects.put(ObjectType.BLOB, encode(report.model_dump(mode="json")))
        self.history._ref(connection, repository, blob, "debug_report", str(identity), "report")
        connection.execute(
            TABLES["debug_reports"]
            .insert()
            .values(
                id=identity,
                repository_id=repository,
                failed_run_id=UUID(report.failed.run_id),
                reference_run_id=UUID(report.reference.run_id) if report.reference else None,
                report_ref=blob.id,
                actor=actor,
            )
        )
        return {"debug_report_id": str(identity), "report_ref": blob.id, "mode": "static_only"}

    def get(self, repository: UUID, identity: UUID) -> dict[str, Any]:
        table = TABLES["debug_reports"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository, table.c.id == identity
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "定位报告不可访问")
        report = parse_contract(
            self._owned(
                repository,
                "debug_report",
                str(identity),
                "report",
                row["report_ref"],
                ObjectType.BLOB,
            ),
            DebugReport,
        )
        if (
            report.repository_id != str(repository)
            or report.failed.run_id != str(row["failed_run_id"])
            or (report.reference.run_id if report.reference else None)
            != (str(row["reference_run_id"]) if row["reference_run_id"] else None)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "定位报告与所属索引不符")
        return {
            "debug_report_id": str(identity),
            "report_ref": row["report_ref"],
            "created_at": row["created_at"].isoformat(),
            "actor": row["actor"],
            "report": report.model_dump(mode="json"),
        }

    def listing(
        self, repository: UUID, *, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentGitError(ErrorCode.VALIDATION, "报告列表分页大小必须为 1–100")
        table = TABLES["debug_reports"]
        with self.history.engine.connect() as connection:
            exists = connection.execute(
                sa.select(TABLES["repositories"].c.id).where(
                    TABLES["repositories"].c.id == repository
                )
            ).first()
            if exists is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "报告项目不可访问")
            if (
                after_id is not None
                and connection.execute(
                    sa.select(table.c.id).where(
                        table.c.repository_id == repository, table.c.id == after_id
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "报告游标不可访问")
            query = sa.select(table).where(table.c.repository_id == repository)
            if after_id is not None:
                query = query.where(table.c.id > after_id)
            rows = list(connection.execute(query.order_by(table.c.id).limit(limit + 1)).mappings())
        items = [
            {
                "debug_report_id": str(row["id"]),
                "failed_run_id": str(row["failed_run_id"]),
                "reference_run_id": str(row["reference_run_id"])
                if row["reference_run_id"]
                else None,
                "report_ref": row["report_ref"],
                "actor": row["actor"],
                "created_at": row["created_at"].isoformat(),
            }
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_after_id": items[-1]["debug_report_id"] if len(rows) > limit else None,
        }
