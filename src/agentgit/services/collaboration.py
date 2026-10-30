"""原子准备四角色独立任务；私有输入只通过后端持有的有效租约读取。"""

import hashlib
import platform
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit import __version__
from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration import (
    CollaborationBindings,
    CollaborationProgram,
    CollaborationRole,
    RoleBinding,
    RolePolicy,
)
from agentgit.core.collaboration_runs import (
    CollaborationFrozenInput,
    CollaborationRolePayload,
    CollaborationSubmission,
    RolePrivateInput,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenCorpus
from agentgit.core.jobs import Lease
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import BudgetLimits, ResearchGoal
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES

if TYPE_CHECKING:
    from agentgit.services.jobs import JobService


@dataclass(frozen=True)
class PreparedRoleInput:
    bindings: CollaborationBindings
    policy: RolePolicy
    goal: ResearchGoal
    private_input: RolePrivateInput
    environment: EnvironmentManifest
    frozen_input: CollaborationFrozenInput


class CollaborationService:
    def __init__(self, queue: "JobService"):
        self.queue, self.history = queue, queue.history

    def prepare(
        self,
        repository_id: UUID,
        program: CollaborationProgram,
        *,
        actor: str,
        key: str,
        private_inputs: tuple[RolePrivateInput, ...] = (),
    ) -> CollaborationSubmission:
        program = parse_contract(encode(program.model_dump(mode="json")), CollaborationProgram)
        if not actor.strip() or not key.strip() or len(actor) > 200 or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "协作操作者及原键必须有界且非空")
        supplied = tuple(
            parse_contract(encode(p.model_dump(mode="json")), RolePrivateInput)
            for p in private_inputs
        )
        if len({p.role for p in supplied}) != len(supplied):
            raise AgentGitError(ErrorCode.VALIDATION, "同一角色不能重复指定私有输入")
        private = {
            role: next((p for p in supplied if p.role == role), RolePrivateInput(role=role))
            for role in CollaborationRole
        }
        for role, item in private.items():
            if (
                len(encode(item.model_dump(mode="json")))
                > program.policy(role).limits.max_context_bytes
            ):
                raise AgentGitError(ErrorCode.CONTEXT_LIMIT, "角色私有输入超过其独立上下文预算")
        store = self.history.objects
        program_obj = store.put_json(ObjectType.BLOB, program.model_dump(mode="json"))
        private_objects = {
            role: store.put_json(ObjectType.BLOB, p.model_dump(mode="json"))
            for role, p in private.items()
        }
        corpus = self.queue.research.corpus
        if set(program.goal.frameworks) != {source.framework for source in corpus.sources.values()}:
            raise AgentGitError(ErrorCode.VALIDATION, "协作目标必须匹配冻结的三框架语料")
        frozen_corpus = FrozenCorpus(
            evaluation_time=corpus.evaluation_time,
            sources=tuple(corpus.sources[k] for k in sorted(corpus.sources)),
        )
        corpus_obj = store.put_json(ObjectType.BLOB, frozen_corpus.model_dump(mode="json"))
        revision, lock = source_revision(), dependency_lock_digest()
        request_digest = object_digest(
            ObjectType.BLOB,
            encode(
                dict(
                    schema_version=1,
                    program_ref=program_obj.id,
                    private_inputs={role.value: obj.id for role, obj in private_objects.items()},
                    corpus_ref=corpus_obj.id,
                    code_revision=revision,
                    dependency_lock_digest=lock,
                    runtime_version=__version__,
                    python_version=platform.python_version(),
                )
            ),
        )
        route = f"POST /repositories/{repository_id}/collaborations/prepare"
        with self.history.engine.begin() as connection:
            connection.execute(
                sa.select(
                    sa.func.pg_advisory_xact_lock(
                        sa.func.hashtextextended(encode([actor, route, key]).decode(), 0)
                    )
                )
            )
            idem = TABLES["idempotency_keys"]
            cached = (
                connection.execute(
                    sa.select(idem).where(
                        idem.c.actor == actor, idem.c.route == route, idem.c.key == key
                    )
                )
                .mappings()
                .first()
            )
            if cached is not None:
                if (
                    cached["repository_id"] != repository_id
                    or cached["request_digest"] != request_digest
                ):
                    raise AgentGitError(
                        ErrorCode.VERSION_CONFLICT, "协作原键不能更换程序、私有输入或环境"
                    )
                return parse_contract(
                    self._owned(
                        connection,
                        repository_id,
                        cached["result_ref"],
                        "idempotency",
                        str(cached["job_id"]),
                        "response",
                    ),
                    CollaborationSubmission,
                )
            repositories = TABLES["repositories"]
            if (
                connection.execute(
                    sa.select(repositories.c.id)
                    .where(repositories.c.id == repository_id)
                    .with_for_update()
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            group_id = uuid4()
            bindings = CollaborationBindings(
                repository_id=str(repository_id),
                collaboration_id=str(group_id),
                agents=tuple(
                    RoleBinding(role=role, agent_run_id=str(uuid4()), task_id=str(uuid4()))
                    for role in CollaborationRole
                ),
            )
            bindings_obj = store.put_json(ObjectType.BLOB, bindings.model_dump(mode="json"))
            frozen = CollaborationFrozenInput(
                program_ref=program_obj.id,
                bindings_ref=bindings_obj.id,
                corpus_ref=corpus_obj.id,
                corpus_digest=corpus.digest(),
            )
            input_obj = store.put_json(ObjectType.BLOB, frozen.model_dump(mode="json"))
            environment = EnvironmentManifest(
                runtime_version=__version__,
                python_version=platform.python_version(),
                code_revision=revision,
                dependency_lock_digest=lock,
                model_signature="offline-four-role-research/v1",
                prompt_digest=hashlib.sha256(
                    encode(program.goal.model_dump(mode="json"))
                ).hexdigest(),
                input_ref=input_obj.id,
                evaluation_time=corpus.evaluation_time,
                random_seed=0,
            )
            environment_obj = store.put_json(
                ObjectType.ENVIRONMENT, environment.model_dump(mode="json")
            )
            for name, obj in [
                ("program", program_obj),
                ("bindings", bindings_obj),
                ("input", input_obj),
                ("environment", environment_obj),
                ("corpus", corpus_obj),
            ]:
                self.history._ref(
                    connection, repository_id, obj, "collaboration", str(group_id), name
                )
            connection.execute(
                TABLES["collaborations"]
                .insert()
                .values(
                    id=group_id,
                    repository_id=repository_id,
                    program_ref=program_obj.id,
                    bindings_ref=bindings_obj.id,
                    environment_ref=environment_obj.id,
                    actor=actor,
                )
            )
            jobs = {}
            for agent in bindings.agents:
                role, task_id, run_id = agent.role, UUID(agent.task_id), UUID(agent.agent_run_id)
                job_id = uuid4()
                jobs[role] = job_id
                policy = program.policy(role)
                policy_obj = store.put_json(ObjectType.BLOB, policy.model_dump(mode="json"))
                budget_obj = store.put_json(ObjectType.BLOB, policy.limits.model_dump(mode="json"))
                payload = CollaborationRolePayload(
                    collaboration_id=str(group_id),
                    role=role,
                    program_ref=program_obj.id,
                    bindings_ref=bindings_obj.id,
                    environment_ref=environment_obj.id,
                    policy_ref=policy_obj.id,
                    private_input_ref=private_objects[role].id,
                    code_revision=revision,
                    dependency_lock_digest=lock,
                    max_wall_ms=policy.limits.max_wall_ms,
                )
                payload_obj = store.put_json(ObjectType.BLOB, payload.model_dump(mode="json"))
                for name, obj in [
                    ("policy", policy_obj),
                    ("budget", budget_obj),
                    ("private_input", private_objects[role]),
                ]:
                    self.history._ref(
                        connection, repository_id, obj, "collaboration_task", str(task_id), name
                    )
                self.history._ref(
                    connection, repository_id, payload_obj, "job", str(job_id), "payload"
                )
                branch = f"collab/{group_id.hex}/{role.value}"
                connection.execute(
                    TABLES["branches"].insert().values(repository_id=repository_id, name=branch)
                )
                connection.execute(
                    TABLES["runs"]
                    .insert()
                    .values(
                        id=run_id,
                        repository_id=repository_id,
                        branch_name=branch,
                        mode="live",
                        status="paused",
                    )
                )
                connection.execute(
                    TABLES["jobs"]
                    .insert()
                    .values(
                        id=job_id,
                        repository_id=repository_id,
                        run_id=run_id,
                        kind="collaboration_role",
                        status="paused",
                        payload_ref=payload_obj.id,
                    )
                )
                connection.execute(
                    TABLES["collaboration_tasks"]
                    .insert()
                    .values(
                        id=task_id,
                        repository_id=repository_id,
                        collaboration_id=group_id,
                        role=role.value,
                        run_id=run_id,
                        job_id=job_id,
                        policy_ref=policy_obj.id,
                        budget_ref=budget_obj.id,
                        private_input_ref=private_objects[role].id,
                    )
                )
                self.history.fault("collaboration.role_prepared")
            supervisor = next(a for a in bindings.agents if a.role == CollaborationRole.SUPERVISOR)
            result = CollaborationSubmission(
                collaboration_id=str(group_id),
                bindings_ref=bindings_obj.id,
                supervisor_run_id=supervisor.agent_run_id,
                supervisor_job_id=str(jobs[CollaborationRole.SUPERVISOR]),
            )
            result_obj = store.put_json(ObjectType.BLOB, result.model_dump(mode="json"))
            self.history._ref(
                connection,
                repository_id,
                result_obj,
                "idempotency",
                result.supervisor_job_id,
                "response",
            )
            connection.execute(
                idem.insert().values(
                    actor=actor,
                    route=route,
                    key=key,
                    repository_id=repository_id,
                    request_digest=request_digest,
                    result_ref=result_obj.id,
                    job_id=UUID(result.supervisor_job_id),
                )
            )
            self.history.fault("collaboration.before_commit")
        return result

    def _owned(
        self,
        connection: Connection,
        repository_id: UUID,
        ref: str,
        owner: str,
        identity: str,
        role: str,
        kind: ObjectType = ObjectType.BLOB,
    ) -> bytes:
        refs = TABLES["object_refs"]
        if (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository_id,
                    refs.c.object_id == ref,
                    refs.c.owner_type == owner,
                    refs.c.owner_id == identity,
                    refs.c.role == role,
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "协作对象缺少确切输入归属")
        return self.history.objects.get(ref, kind)

    def summary(self, repository_id: UUID, collaboration_id: UUID) -> dict[str, Any]:
        groups, tasks = TABLES["collaborations"], TABLES["collaboration_tasks"]
        with self.history.engine.connect() as connection:
            group = (
                connection.execute(
                    sa.select(groups).where(
                        groups.c.repository_id == repository_id, groups.c.id == collaboration_id
                    )
                )
                .mappings()
                .first()
            )
            if group is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "协作组不可访问")
            agents = (
                connection.execute(
                    sa.select(tasks.c.role, tasks.c.id, tasks.c.run_id, tasks.c.job_id)
                    .where(
                        tasks.c.repository_id == repository_id,
                        tasks.c.collaboration_id == collaboration_id,
                    )
                    .order_by(tasks.c.role)
                )
                .mappings()
                .all()
            )
        return dict(
            collaboration_id=str(collaboration_id),
            status=group["status"],
            program_ref=group["program_ref"],
            bindings_ref=group["bindings_ref"],
            roles=[
                dict(
                    role=a["role"],
                    task_id=str(a["id"]),
                    run_id=str(a["run_id"]),
                    job_id=str(a["job_id"]),
                )
                for a in agents
            ],
        )

    def role_input(self, lease: Lease, *, run_id: UUID | None = None) -> PreparedRoleInput:
        """Lease 仅由可信调度器传入；不接受模型、渲染器或 envelope 自报的身份。"""
        with self.history.engine.begin() as connection:
            job = self.queue.guard(connection, lease, lease.run_id)
            if run_id is not None and run_id != lease.run_id:
                raise AgentGitError(ErrorCode.FORBIDDEN, "角色不能读取其他 Run 的私有输入")
            if job["kind"] != "collaboration_role":
                raise AgentGitError(ErrorCode.FORBIDDEN, "Job 不是协作角色")
            tasks, groups = TABLES["collaboration_tasks"], TABLES["collaborations"]
            row = (
                connection.execute(
                    sa.select(tasks).where(
                        tasks.c.repository_id == lease.repository_id,
                        tasks.c.run_id == lease.run_id,
                        tasks.c.job_id == lease.job_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "角色任务绑定缺失")
            group = (
                connection.execute(
                    sa.select(groups).where(
                        groups.c.repository_id == lease.repository_id,
                        groups.c.id == row["collaboration_id"],
                    )
                )
                .mappings()
                .one()
            )
            repo, identity = lease.repository_id, str(group["id"])
            program = parse_contract(
                self._owned(
                    connection, repo, group["program_ref"], "collaboration", identity, "program"
                ),
                CollaborationProgram,
            )
            bindings = parse_contract(
                self._owned(
                    connection, repo, group["bindings_ref"], "collaboration", identity, "bindings"
                ),
                CollaborationBindings,
            )
            environment = parse_contract(
                self._owned(
                    connection,
                    repo,
                    group["environment_ref"],
                    "collaboration",
                    identity,
                    "environment",
                    ObjectType.ENVIRONMENT,
                ),
                EnvironmentManifest,
            )
            frozen = parse_contract(
                self._owned(
                    connection, repo, environment.input_ref, "collaboration", identity, "input"
                ),
                CollaborationFrozenInput,
            )
            corpus = parse_contract(
                self._owned(
                    connection, repo, frozen.corpus_ref, "collaboration", identity, "corpus"
                ),
                FrozenCorpus,
            )
            all_tasks = (
                connection.execute(
                    sa.select(tasks).where(
                        tasks.c.repository_id == repo, tasks.c.collaboration_id == group["id"]
                    )
                )
                .mappings()
                .all()
            )
            if (
                bindings.repository_id,
                bindings.collaboration_id,
                frozen.program_ref,
                frozen.bindings_ref,
            ) != (str(repo), identity, group["program_ref"], group["bindings_ref"]) or {
                (a.role.value, a.agent_run_id, a.task_id) for a in bindings.agents
            } != {(a["role"], str(a["run_id"]), str(a["id"])) for a in all_tasks}:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "协作冻结绑定与实际角色任务不符")
            if (
                environment.code_revision != source_revision()
                or environment.dependency_lock_digest != dependency_lock_digest()
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "协作代码或依赖锁已变化")
            if (
                environment.runtime_version != __version__
                or environment.python_version != platform.python_version()
                or environment.model_signature != "offline-four-role-research/v1"
                or environment.prompt_digest
                != hashlib.sha256(encode(program.goal.model_dump(mode="json"))).hexdigest()
                or environment.tool_signatures
                or environment.random_seed != 0
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "协作环境签名或固定输入不符")
            if (
                Corpus(corpus.sources, evaluation_time=corpus.evaluation_time).digest()
                != frozen.corpus_digest
                or corpus.evaluation_time != environment.evaluation_time
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "协作冻结语料或有效时间不符")
            task_id = str(row["id"])
            policy = parse_contract(
                self._owned(
                    connection, repo, row["policy_ref"], "collaboration_task", task_id, "policy"
                ),
                RolePolicy,
            )
            budget = parse_contract(
                self._owned(
                    connection, repo, row["budget_ref"], "collaboration_task", task_id, "budget"
                ),
                BudgetLimits,
            )
            private = parse_contract(
                self._owned(
                    connection,
                    repo,
                    row["private_input_ref"],
                    "collaboration_task",
                    task_id,
                    "private_input",
                ),
                RolePrivateInput,
            )
            payload = parse_contract(
                self._owned(
                    connection, repo, job["payload_ref"], "job", str(lease.job_id), "payload"
                ),
                CollaborationRolePayload,
            )
            expected = CollaborationRolePayload(
                collaboration_id=identity,
                role=CollaborationRole(row["role"]),
                program_ref=group["program_ref"],
                bindings_ref=group["bindings_ref"],
                environment_ref=group["environment_ref"],
                policy_ref=row["policy_ref"],
                private_input_ref=row["private_input_ref"],
                code_revision=environment.code_revision,
                dependency_lock_digest=environment.dependency_lock_digest,
                max_wall_ms=policy.limits.max_wall_ms,
            )
            if (
                payload != expected
                or policy != program.policy(payload.role)
                or budget != policy.limits
                or private.role != payload.role
                or len(encode(private.model_dump(mode="json"))) > policy.limits.max_context_bytes
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "角色载荷、预算、策略或私有输入不符")
            return PreparedRoleInput(bindings, policy, program.goal, private, environment, frozen)
