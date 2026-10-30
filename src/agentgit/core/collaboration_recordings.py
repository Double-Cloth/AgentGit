"""完整成功协作来源的有界索引；索引核验不等于执行重放。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.collaboration import CollaborationRole
from agentgit.core.models import Contract, Digest, Identifier


class CollaborationSourceFrame(Contract):
    commit_id: Digest
    state_ref: Digest
    environment_ref: Digest
    event_manifest_ref: Digest
    first_seq: Annotated[int, Field(ge=1)]
    last_seq: Annotated[int, Field(ge=1)]


class CollaborationRoleSource(Contract):
    role: CollaborationRole
    task_id: Identifier
    run_id: Identifier
    job_id: Identifier
    payload_ref: Digest
    policy_ref: Digest
    budget_ref: Digest
    private_input_ref: Digest
    frames: Annotated[tuple[CollaborationSourceFrame, ...], Field(min_length=1, max_length=10000)]
    call_ids: Annotated[tuple[Identifier, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def complete_path(self) -> "CollaborationRoleSource":
        previous = 0
        for frame in self.frames:
            if frame.first_seq != previous + 1 or frame.last_seq < frame.first_seq:
                raise ValueError("角色来源必须从首事件开始且连续覆盖")
            previous = frame.last_seq
        if len({f.commit_id for f in self.frames}) != len(self.frames):
            raise ValueError("角色来源不能重复版本")
        if len(set(self.call_ids)) != len(self.call_ids):
            raise ValueError("角色来源不能重复调用")
        return self


class CollaborationSourceIndex(Contract):
    kind: Literal["collaboration-source-index/v1"] = "collaboration-source-index/v1"
    repository_id: Identifier
    collaboration_id: Identifier
    program_ref: Digest
    bindings_ref: Digest
    environment_ref: Digest
    input_ref: Digest
    corpus_ref: Digest
    code_revision: str
    dependency_lock_digest: Digest
    roles: Annotated[tuple[CollaborationRoleSource, ...], Field(min_length=4, max_length=4)]
    message_ids: Annotated[tuple[Identifier, ...], Field(min_length=7, max_length=7)]
    shared_head_refs: Annotated[tuple[Digest, ...], Field(min_length=5, max_length=5)]
    object_refs: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=100000)]
    total_bytes: Annotated[int, Field(ge=1, le=64 * 1024 * 1024)]

    @model_validator(mode="after")
    def distinct_scope(self) -> "CollaborationSourceIndex":
        if tuple(r.role for r in self.roles) != tuple(CollaborationRole):
            raise ValueError("来源必须按固定顺序包含四个独立角色")
        for name in ("task_id", "run_id", "job_id"):
            if len({getattr(r, name) for r in self.roles}) != 4:
                raise ValueError("角色来源身份必须独立")
        if len(set(self.message_ids)) != 7 or len(set(self.shared_head_refs)) != 5:
            raise ValueError("交换来源不能重复")
        if self.object_refs != tuple(sorted(set(self.object_refs))):
            raise ValueError("来源对象闭包必须去重并排序")
        if sum(r.frames[-1].last_seq for r in self.roles) > 50000:
            raise ValueError("协作来源事件超过有界上限")
        return self
