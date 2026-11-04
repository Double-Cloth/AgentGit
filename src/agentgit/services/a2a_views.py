"""A2A 本人内容查询与明确的本地／远端、历史／实际投影；不建立协议连接。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.a2a import A2A_RELEASE, A2A_VERSION
from agentgit.core.a2a_runs import A2ARecordedSpec, A2ARunState
from agentgit.core.a2a_wire import A2AResult, binding_digest
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.core.snapshots import StateSnapshot
from agentgit.services.a2a_runs import A2ARunService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService


class A2AViewService:
    def __init__(self, queue: JobService):
        self.queue, self.history = queue, queue.history
        self.base = A2ARunService(queue)

    def authorize_run(self, repo: UUID, run: UUID, *, actor: str) -> bool:
        jobs, keys = TABLES["jobs"], TABLES["idempotency_keys"]
        with self.history.engine.connect() as c:
            job = c.execute(
                sa.select(jobs.c.id).where(
                    jobs.c.repository_id == repo, jobs.c.run_id == run, jobs.c.kind == "a2a_run"
                )
            ).scalar_one_or_none()
            if job is None:
                return False
            owned = c.execute(
                sa.select(keys.c.job_id).where(
                    keys.c.repository_id == repo,
                    keys.c.job_id == job,
                    keys.c.actor == actor,
                    keys.c.route == f"POST /repositories/{repo}/a2a-runs",
                )
            ).first()
        if owned is None:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 内容及控制只允许所属操作者访问")
        self.base.owned(repo, run, actor)
        return True

    def authorize_commit(self, repo: UUID, commit: str, *, actor: str) -> bool:
        snapshots, jobs = TABLES["snapshots"], TABLES["jobs"]
        with self.history.engine.connect() as c:
            runs = (
                c.execute(
                    sa.select(jobs.c.run_id)
                    .join(
                        snapshots,
                        sa.and_(
                            snapshots.c.repository_id == jobs.c.repository_id,
                            snapshots.c.run_id == jobs.c.run_id,
                        ),
                    )
                    .where(
                        jobs.c.repository_id == repo,
                        jobs.c.kind == "a2a_run",
                        snapshots.c.commit_id == commit,
                    )
                    .limit(101)
                )
                .scalars()
                .all()
            )
        if len(runs) > 100:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "A2A 共享历史归属超过查询上限")
        for run in runs:
            try:
                self.authorize_run(repo, run, actor=actor)
                return True
            except AgentGitError as error:
                if error.code != ErrorCode.FORBIDDEN:
                    raise
        if runs:
            raise AgentGitError(ErrorCode.FORBIDDEN, "A2A 历史只允许所属操作者访问")
        return False

    def listing(
        self, repo: UUID, *, actor: str, after_id: UUID | None = None, limit: int = 30
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentGitError(ErrorCode.VALIDATION, "A2A 查询每页必须为1至100项")
        self.history.branch(repo)
        runs, jobs, keys = TABLES["runs"], TABLES["jobs"], TABLES["idempotency_keys"]
        owned = sa.exists(
            sa.select(keys.c.job_id).where(
                keys.c.repository_id == repo,
                keys.c.job_id == jobs.c.id,
                keys.c.actor == actor,
                keys.c.route == f"POST /repositories/{repo}/a2a-runs",
            )
        )
        query = (
            sa.select(
                runs.c.id, jobs.c.id.label("job_id"), runs.c.status, runs.c.mode, runs.c.created_at
            )
            .join(
                jobs,
                sa.and_(jobs.c.run_id == runs.c.id, jobs.c.repository_id == runs.c.repository_id),
            )
            .where(runs.c.repository_id == repo, jobs.c.kind == "a2a_run", owned)
        )
        with self.history.engine.connect() as c:
            if after_id is not None:
                anchor = c.execute(query.where(runs.c.id == after_id)).mappings().first()
                if anchor is None:
                    raise AgentGitError(ErrorCode.NOT_FOUND, "A2A 游标不属于本人可见任务")
                query = query.where(
                    sa.tuple_(runs.c.created_at, runs.c.id)
                    < sa.tuple_(anchor["created_at"], anchor["id"])
                )
            rows = (
                c.execute(
                    query.order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(limit + 1)
                )
                .mappings()
                .all()
            )
        for row in rows[:limit]:
            self.base.owned(repo, row["id"], actor)
        return dict(
            items=[dict(r) for r in rows[:limit]],
            next_after_id=rows[limit - 1]["id"] if len(rows) > limit else None,
        )

    def program(self, repo: UUID, run: UUID, *, actor: str) -> dict[str, Any]:
        self.authorize_run(repo, run, actor=actor)
        payload = self.base.owned(repo, run, actor)
        return self.base.program(repo, payload).model_dump(mode="json")

    def summary(self, repo: UUID, run_id: UUID, *, actor: str) -> dict[str, Any]:
        self.authorize_run(repo, run_id, actor=actor)
        payload = self.base.owned(repo, run_id, actor)
        program = self.base.program(repo, payload)
        run = self.queue.run(repo, run_id)
        if run["mode"] != ("recorded" if payload.replay else "live"):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A Run模式与载荷不符")
        local = self.base.recorded_state(repo, run_id, actor=actor) if payload.replay else None
        logical = (
            local.logical_state
            if local
            else (self.base.state(repo, run_id, actor=actor) if payload.replay is None else None)
        )
        initialized = logical is not None
        logical = logical or A2ARunState(program_ref=payload.program_ref)
        logical_ref = None
        physical_ref = None
        if initialized:
            snapshot = self.history.restore(repo, run["checkpoint_commit_id"])
            physical_ref = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
            projected = StateSnapshot(
                data=logical.logical_projection(), artifacts=snapshot.artifacts
            )
            logical_ref = object_digest(ObjectType.STATE, encode(projected.model_dump(mode="json")))
        task_id = context_id = None
        if logical.last_response_ref is not None:
            result = parse_contract(
                self.history.objects.get(logical.last_response_ref, ObjectType.BLOB), A2AResult
            )
            if result.binding_ref != binding_digest(program.binding):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "A2A 显示回执与固定身份绑定不符")
            task_id, context_id = result.reference().task_id, result.reference().context_id
        calls, paths = TABLES["external_calls"], TABLES["run_commits"]
        with self.history.engine.connect() as c:
            rows = (
                c.execute(
                    sa.select(calls)
                    .where(calls.c.repository_id == repo, calls.c.run_id == run_id)
                    .limit(65)
                )
                .mappings()
                .all()
            )
            commit_count = c.execute(
                sa.select(sa.func.count())
                .select_from(paths)
                .where(paths.c.repository_id == repo, paths.c.run_id == run_id)
            ).scalar_one()
            first_commit = c.execute(
                sa.select(paths.c.commit_id).where(
                    paths.c.repository_id == repo, paths.c.run_id == run_id, paths.c.run_order == 1
                )
            ).scalar_one_or_none()
        if len(rows) > 64:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "A2A 运行索引超过固定查询上限")
        unknown = any(
            r["result_ref"] is None or r["status"] in {"started", "outcome_unknown"} for r in rows
        )
        response_bytes = 0
        for row in rows:
            if row["result_ref"] is not None:
                saved = parse_contract(
                    self.history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
                )
                unknown = unknown or not saved.complete
                response_bytes += saved.response_bytes
        if payload.replay is not None and rows:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 混入实际调用")
        candidate = None
        reason = None
        if payload.replay is None and run["status"] == "completed":
            if (
                payload.code_revision != source_revision()
                or payload.dependency_lock_digest != dependency_lock_digest()
            ):
                reason = ErrorCode.UNSUPPORTED_SCHEMA.value
            elif (
                logical.retired
                or first_commit is None
                or commit_count != 1 + 2 * len(logical.observations)
            ):
                reason = ErrorCode.RECORDING_MISSING.value
            else:
                candidate = A2ARecordedSpec(
                    source_run_id=str(run_id),
                    from_commit=first_commit,
                    to_commit=run["checkpoint_commit_id"],
                ).model_dump(mode="json")
        return dict(
            kind="a2a_program/v1",
            program_ref=payload.program_ref,
            mode=run["mode"],
            release=A2A_RELEASE,
            protocol_version=A2A_VERSION,
            binding_ref=binding_digest(program.binding),
            server_identity=program.binding.server.server_identity,
            principal_id=program.binding.server.principal_id,
            endpoint=program.binding.server.endpoint,
            has_model=False,
            limits=program.limits.model_dump(mode="json"),
            logical_usage=logical.usage.model_dump(mode="json"),
            actual_attempts=None if unknown else len(rows),
            actual_response_bytes=None if unknown else response_bytes,
            registered_attempts=len(rows),
            local_status=run["status"],
            remote_state=logical.remote_state,
            waiting_for=logical.waiting_for,
            remote_cancel_status=logical.remote_cancel_status,
            response_ref=logical.last_response_ref,
            task_id=task_id,
            context_id=context_id,
            logical_state_ref=logical_ref,
            physical_state_ref=physical_ref,
            remote_identity_scope="historical_source" if payload.replay else "last_confirmed",
            control_scope="local_recorded"
            if payload.replay
            else "local_pause_separate_remote_cancel_approval",
            initialized=initialized,
            cursor=local.cursor if local else None,
            recording=payload.replay.model_dump(mode="json") if payload.replay else None,
            recording_candidate=candidate,
            recording_eligibility="requires_verification" if candidate else None,
            recording_unavailable_reason=reason,
        )
