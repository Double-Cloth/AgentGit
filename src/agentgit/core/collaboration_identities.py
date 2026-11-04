"""协作 Recorded 的类型化新身份；不对私有文本或对象字节做替换。"""

from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationBindings,
    CollaborationMessage,
    CollaborationRole,
)
from agentgit.core.collaboration_execution import CollaborationRoleState
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, Identifier
from agentgit.core.research import evolve


class RoleJobIdentity(Contract):
    role: CollaborationRole
    source_job_id: Identifier
    target_job_id: Identifier


class CollaborationIdentityMap(Contract):
    kind: Literal["collaboration-identity-map/v1"] = "collaboration-identity-map/v1"
    source: CollaborationBindings
    target: CollaborationBindings
    jobs: Annotated[tuple[RoleJobIdentity, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def independent_targets(self) -> "CollaborationIdentityMap":
        if (
            self.source.repository_id != self.target.repository_id
            or self.source.collaboration_id == self.target.collaboration_id
        ):
            raise ValueError("Recorded 必须在同项目创建独立协作身份")
        if tuple(j.role for j in self.jobs) != tuple(CollaborationRole):
            raise ValueError("Recorded 必须按固定角色顺序映射四个 Job")
        for name in ("agent_run_id", "task_id"):
            if {getattr(a, name) for a in self.source.agents} & {
                getattr(a, name) for a in self.target.agents
            }:
                raise ValueError("Recorded 不能复用任一来源 Run/任务")
        old, new = {j.source_job_id for j in self.jobs}, {j.target_job_id for j in self.jobs}
        if len(old) != 4 or len(new) != 4 or old & new:
            raise ValueError("Recorded 的四个 Job 必须独立且不能复用来源")
        return self

    def run(self, source_run: str) -> str:
        role = self.source.agent(source_run).role
        return next(a.agent_run_id for a in self.target.agents if a.role == role)

    def message_id(self, source_id: str) -> str:
        routes = (
            ("supervisor", "dispatch", "researcher"),
            ("supervisor", "dispatch", "verifier"),
            ("supervisor", "dispatch", "writer"),
            ("researcher", "evidence", "verifier"),
            ("researcher", "evidence", "writer"),
            ("verifier", "verification", "writer"),
            ("writer", "result", "supervisor"),
        )
        for sender, kind, receiver in routes:
            name = f"{sender}:{kind}:{receiver}"
            if source_id == str(uuid5(UUID(self.source.collaboration_id), name)):
                return str(uuid5(UUID(self.target.collaboration_id), name))
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "消息不属于固定成功来源")

    def message(
        self, source: CollaborationMessage, parent: CausalEventAddress
    ) -> CollaborationMessage:
        receiver = self.source.agent(source.receiver)
        sender = self.source.agent(source.sender)
        target = next(a for a in self.target.agents if a.role == receiver.role)
        if (
            (source.repository_id, source.collaboration_id, source.correlation_id, source.task_id)
            != (
                self.source.repository_id,
                self.source.collaboration_id,
                self.source.collaboration_id,
                receiver.task_id,
            )
            or source.parent_event_id.agent_run_id != source.sender
            or parent.agent_run_id != self.run(source.sender)
            or source.message_id
            != str(
                uuid5(
                    UUID(self.source.collaboration_id),
                    f"{sender.role.value}:{source.kind}:{receiver.role.value}",
                )
            )
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "消息或新父地址不属于确切角色映射")
        return evolve(
            source,
            repository_id=self.target.repository_id,
            collaboration_id=self.target.collaboration_id,
            message_id=self.message_id(source.message_id),
            sender=self.run(source.sender),
            receiver=target.agent_run_id,
            task_id=target.task_id,
            correlation_id=self.target.collaboration_id,
            parent_event_id=parent,
        )

    def state(
        self, source: CollaborationRoleState, parents: dict[CausalEventAddress, CausalEventAddress]
    ) -> CollaborationRoleState:
        if source.collaboration_id != self.source.collaboration_id:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "角色状态不属于固定来源组")
        publications = []
        for head in source.publications:
            if head.publisher_event not in parents:
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "共享版本缺少已保存的新导出父地址")
            parent = parents[head.publisher_event]
            if (
                (head.repository_id, head.collaboration_id, head.program_ref, head.bindings_ref)
                != (
                    self.source.repository_id,
                    self.source.collaboration_id,
                    source.program_ref,
                    object_digest(ObjectType.BLOB, encode(self.source.model_dump(mode="json"))),
                )
                or parent.agent_run_id != self.run(head.publisher_event.agent_run_id)
                or self.source.agent(head.publisher_event.agent_run_id).role != source.role
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "共享版本或新父地址不属于确切来源")
            publications.append(
                evolve(
                    head,
                    repository_id=self.target.repository_id,
                    collaboration_id=self.target.collaboration_id,
                    publisher_event=parent,
                    bindings_ref=object_digest(
                        ObjectType.BLOB, encode(self.target.model_dump(mode="json"))
                    ),
                )
            )
        return evolve(
            source,
            collaboration_id=self.target.collaboration_id,
            sent=tuple(self.message_id(m) for m in source.sent),
            received=tuple(self.message_id(m) for m in source.received),
            publications=tuple(publications),
        )
