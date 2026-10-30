"""复用耐久事件和原键收据的组控制；不复制权限、预算或角色私有上下文。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration_controls import (
    CollaborationControlFact,
    CollaborationControlReceipt,
    CollaborationControlRequest,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.snapshots import EventPayload
from agentgit.services.collaboration_control import wake_stopped
from agentgit.services.collaboration_exchange import TERMINAL, wake_collaboration
from agentgit.services.history import TABLES, EventInput
from agentgit.services.jobs import JobService

GROUP_EVENT = "collaboration.group_control_requested"


class CollaborationControlService:
    def __init__(self, queue: JobService):
        self.queue, self.history = queue, queue.history

    def _scope(self, c: Connection, repo: UUID, group: UUID, actor: str) -> dict[str, Any]:
        table = TABLES["collaborations"]
        row = (
            c.execute(sa.select(table).where(table.c.repository_id == repo, table.c.id == group))
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "协作组不可访问")
        if row["actor"] != actor:
            raise AgentGitError(ErrorCode.FORBIDDEN, "组控制身份与准备操作者不符")
        return dict(row)

    def _members(self, c: Connection, repo: UUID, group: UUID) -> list[dict[str, Any]]:
        tasks, runs, jobs = (TABLES[n] for n in ("collaboration_tasks", "runs", "jobs"))
        rows = (
            c.execute(
                sa.select(
                    tasks.c.role,
                    tasks.c.run_id,
                    tasks.c.job_id,
                    runs.c.status,
                    runs.c.control_request,
                    runs.c.control_status,
                    runs.c.last_event_seq,
                    jobs.c.status.label("job_status"),
                )
                .join(
                    runs,
                    sa.and_(
                        runs.c.repository_id == tasks.c.repository_id, runs.c.id == tasks.c.run_id
                    ),
                )
                .join(
                    jobs,
                    sa.and_(
                        jobs.c.repository_id == tasks.c.repository_id, jobs.c.id == tasks.c.job_id
                    ),
                )
                .where(tasks.c.repository_id == repo, tasks.c.collaboration_id == group)
                .order_by(tasks.c.run_id)
            )
            .mappings()
            .all()
        )
        if len(rows) != 4 or {r["role"] for r in rows} != {
            "supervisor",
            "researcher",
            "verifier",
            "writer",
        }:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组控制需要实际四角色绑定")
        return [dict(r) for r in rows]

    def _facts(
        self,
        c: Connection,
        repo: UUID,
        group: UUID,
        supervisor: UUID,
        actor: str,
        members: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], CollaborationControlFact]]:
        events = TABLES["events"]
        rows = (
            c.execute(
                sa.select(events)
                .where(
                    events.c.repository_id == repo,
                    events.c.run_id == supervisor,
                    events.c.type == GROUP_EVENT,
                )
                .order_by(events.c.seq)
            )
            .mappings()
            .all()
        )
        result = []
        for revision, row in enumerate(rows, 1):
            payload = parse_contract(
                self.history.objects.get(row["payload_ref"], ObjectType.EVENT), EventPayload
            )
            fact = parse_contract(encode(payload.data), CollaborationControlFact)
            if (
                fact.collaboration_id != str(group)
                or fact.revision != revision
                or fact.actor != actor
                or set(fact.managed_runs) - {str(m["run_id"]) for m in members}
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组控制事件版本或归属不连续")
            result.append((dict(row), fact))
        return result

    @staticmethod
    def _quiescent(members: list[dict[str, Any]]) -> bool:
        return all(
            m["job_status"] not in ("queued", "running")
            and (m["status"] == "paused" or m["status"] in TERMINAL)
            for m in members
        )

    def status(self, repo: UUID, group: UUID, *, actor: str) -> dict[str, Any]:
        with self.history.engine.connect() as c:
            scope = self._scope(c, repo, group, actor)
            members = self._members(c, repo, group)
            supervisor = next(m["run_id"] for m in members if m["role"] == "supervisor")
            facts = self._facts(c, repo, group, supervisor, actor, members)
        return dict(
            collaboration_id=str(group),
            status=scope["status"],
            revision=len(facts),
            quiescent=self._quiescent(members),
            all_terminal=all(m["status"] in TERMINAL for m in members),
            has_unknown=any(m["status"] == "outcome_unknown" for m in members),
            has_failed=any(m["status"] in ("failed", "budget_exhausted") for m in members),
            roles=[
                {
                    k: str(v) if isinstance(v, UUID) else v
                    for k, v in m.items()
                    if k != "last_event_seq"
                }
                for m in members
            ],
        )

    def _append(self, c: Connection, repo: UUID, run: UUID, event: EventInput) -> tuple[int, str]:
        current = self.history._run(c, repo, run)
        seq = int(current["last_event_seq"]) + 1
        obj = self.history.objects.put_json(
            ObjectType.EVENT, EventPayload(data=event.payload).model_dump(mode="json")
        )
        self.history._event(c, repo, run, seq, event, obj)
        c.execute(
            TABLES["runs"].update().where(TABLES["runs"].c.id == run).values(last_event_seq=seq)
        )
        return seq, obj.id

    def _last_control(self, c: Connection, repo: UUID, run: UUID) -> dict[str, Any] | None:
        events = TABLES["events"]
        row = (
            c.execute(
                sa.select(events)
                .where(
                    events.c.repository_id == repo,
                    events.c.run_id == run,
                    events.c.type == "control.requested",
                )
                .order_by(events.c.seq.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        return None if row is None else dict(row)

    def request(
        self, repo: UUID, group: UUID, request: CollaborationControlRequest, *, actor: str, key: str
    ) -> CollaborationControlReceipt:
        request = parse_contract(
            encode(request.model_dump(mode="json")), CollaborationControlRequest
        )
        if not actor.strip() or not key.strip() or len(actor) > 200 or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "组控制必须提供有界操作者及原键")
        route = f"POST /repositories/{repo}/collaborations/{group}/control"
        digest = object_digest(
            ObjectType.BLOB,
            encode(dict(collaboration_id=str(group), request=request.model_dump(mode="json"))),
        )
        groups, runs, jobs, idem = (
            TABLES[n] for n in ("collaborations", "runs", "jobs", "idempotency_keys")
        )
        with self.history.engine.begin() as c:
            self._scope(c, repo, group, actor)
            c.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(encode([actor, route, key]).decode(), 0)
                    )
                )
            )
            cached = (
                c.execute(
                    sa.select(idem).where(
                        idem.c.actor == actor, idem.c.route == route, idem.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached is not None:
                if cached["request_digest"] != digest:
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "组控制原键不能更换输入")
                receipt = parse_contract(
                    self.history.objects.get(cached["result_ref"], ObjectType.BLOB),
                    CollaborationControlReceipt,
                )
                if (
                    receipt.collaboration_id != str(group)
                    or receipt.action != request.action
                    or receipt.revision != request.expected_revision + 1
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组控制原键收据与请求不符")
                supervisor = next(
                    m for m in self._members(c, repo, group) if m["role"] == "supervisor"
                )
                events = TABLES["events"]
                source = c.execute(
                    sa.select(events.c.payload_ref).where(
                        events.c.repository_id == repo,
                        events.c.run_id == supervisor["run_id"],
                        events.c.seq == receipt.source_seq,
                        events.c.type == GROUP_EVENT,
                        events.c.payload_ref == receipt.source_payload_ref,
                    )
                ).scalar_one_or_none()
                if receipt.source_run_id != str(supervisor["run_id"]) or source is None:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组控制收据缺少实际所属事件")
                source_payload = parse_contract(
                    self.history.objects.get(source, ObjectType.EVENT), EventPayload
                )
                fact = parse_contract(encode(source_payload.data), CollaborationControlFact)
                if (fact.collaboration_id, fact.actor, fact.action, fact.revision) != (
                    str(group),
                    actor,
                    receipt.action,
                    receipt.revision,
                ):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组控制收据与实际事件内容不符")
                return receipt
            members = self._members(c, repo, group)
            # 与 Worker 保持 Run→Job→组的锁顺序，不反向等待活动角色。
            c.execute(
                sa.select(runs.c.id)
                .where(runs.c.repository_id == repo, runs.c.id.in_([m["run_id"] for m in members]))
                .order_by(runs.c.id)
                .with_for_update()
            ).all()
            c.execute(
                sa.select(jobs.c.id)
                .where(jobs.c.repository_id == repo, jobs.c.id.in_([m["job_id"] for m in members]))
                .order_by(jobs.c.id)
                .with_for_update()
            ).all()
            scope = dict(
                c.execute(
                    sa.select(groups)
                    .where(groups.c.repository_id == repo, groups.c.id == group)
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            members = self._members(c, repo, group)
            supervisor = next(m for m in members if m["role"] == "supervisor")
            facts = self._facts(c, repo, group, supervisor["run_id"], actor, members)
            if len(facts) != request.expected_revision:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "组控制版本已经改变")
            allowed = {
                "pause": {"running"},
                "resume": {"paused"},
                "cancel": {"prepared", "running", "paused"},
            }
            if scope["status"] not in allowed[request.action]:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "当前协作状态不接受该组控制")
            if request.action != "cancel" and any(
                m["status"] in TERMINAL - {"completed"} for m in members
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "已有角色停止或未知结果，不能按正常暂停/恢复处理"
                )
            if request.action == "resume" and not self._quiescent(members):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "组暂停尚未安全落实，不能恢复")
            managed = []
            previous = facts[-1] if facts else None
            if request.action == "resume" and (previous is None or previous[1].action != "pause"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "组恢复缺少确切暂停来源")
            for member in members:
                if member["status"] in TERMINAL:
                    continue
                if (
                    request.action == "pause"
                    and member["control_request"] == "pause"
                    and member["control_status"] in ("requested", "applied")
                ):
                    continue
                if request.action == "resume":
                    assert previous is not None
                    last = self._last_control(c, repo, member["run_id"])
                    if (
                        str(member["run_id"]) not in previous[1].managed_runs
                        or last is None
                        or last["caused_by_run_id"] != supervisor["run_id"]
                        or last["caused_by_seq"] != previous[0]["seq"]
                        or member["control_request"] != "pause"
                    ):
                        continue
                managed.append(member)
            fact = CollaborationControlFact(
                collaboration_id=str(group),
                revision=len(facts) + 1,
                action=request.action,
                actor=actor,
                managed_runs=tuple(str(m["run_id"]) for m in managed),
            )
            seq, ref = self._append(
                c,
                repo,
                supervisor["run_id"],
                EventInput(GROUP_EVENT, "collaboration-control", fact.model_dump(mode="json")),
            )
            status = {"pause": "paused", "resume": "running", "cancel": "cancelled"}[request.action]
            values: dict[str, Any] = dict(status=status)
            if request.action == "cancel":
                values.update(failure_run_id=supervisor["run_id"], failure_event_seq=seq)
            c.execute(groups.update().where(groups.c.id == group).values(**values))
            for member in managed:
                inactive_pause = (
                    request.action == "pause"
                    and member["job_status"] != "running"
                    and member["status"] == "paused"
                )
                self._append(
                    c,
                    repo,
                    member["run_id"],
                    EventInput(
                        "control.requested",
                        "control",
                        dict(
                            action=request.action,
                            collaboration_id=str(group),
                            group_revision=fact.revision,
                            already_paused=inactive_pause,
                        ),
                        caused_by_run_id=supervisor["run_id"],
                        caused_by_seq=seq,
                    ),
                )
                c.execute(
                    runs.update()
                    .where(runs.c.id == member["run_id"])
                    .values(
                        control_request=request.action,
                        control_status="applied" if inactive_pause else "requested",
                    )
                )
            if request.action == "pause":
                for member in members:
                    if member["status"] in TERMINAL or member["job_status"] == "running":
                        continue
                    c.execute(
                        jobs.update()
                        .where(jobs.c.id == member["job_id"])
                        .values(
                            status="paused" if member["status"] == "paused" else "queued",
                            worker=None,
                            lease_until=None,
                            available_at=sa.func.clock_timestamp(),
                        )
                    )
            elif request.action == "resume":
                wake_collaboration(self.queue, c, repo, group)
            else:
                wake_stopped(c, repo, group)
            receipt = CollaborationControlReceipt(
                collaboration_id=str(group),
                action=request.action,
                revision=fact.revision,
                source_run_id=str(supervisor["run_id"]),
                source_seq=seq,
                source_payload_ref=ref,
            )
            obj = self.history.objects.put_json(ObjectType.BLOB, receipt.model_dump(mode="json"))
            self.history._ref(c, repo, obj, "idempotency", str(supervisor["job_id"]), "response")
            c.execute(
                idem.insert().values(
                    actor=actor,
                    route=route,
                    key=key,
                    repository_id=repo,
                    request_digest=digest,
                    result_ref=obj.id,
                    job_id=supervisor["job_id"],
                )
            )
            self.history.fault("collaboration.control_projected")
        self.history.fault("collaboration.control_committed")
        return receipt
