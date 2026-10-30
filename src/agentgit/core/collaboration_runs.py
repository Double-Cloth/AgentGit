"""四角色耐久准备的输入、租约载荷和操作者收据。"""

from typing import Annotated, Literal

from pydantic import Field

from agentgit.core.collaboration import CollaborationRole
from agentgit.core.models import Contract, Digest, Identifier


class RolePrivateInput(Contract):
    kind: Literal["collaboration-private-input/v1"] = "collaboration-private-input/v1"
    role: CollaborationRole
    notes: Annotated[tuple[Annotated[str, Field(max_length=4096)], ...], Field(max_length=16)] = ()


class CollaborationFrozenInput(Contract):
    kind: Literal["collaboration-frozen-input/v1"] = "collaboration-frozen-input/v1"
    program_ref: Digest
    bindings_ref: Digest
    corpus_ref: Digest
    corpus_digest: Digest


class CollaborationRolePayload(Contract):
    kind: Literal["collaboration_role"] = "collaboration_role"
    collaboration_id: Identifier
    role: CollaborationRole
    program_ref: Digest
    bindings_ref: Digest
    environment_ref: Digest
    policy_ref: Digest
    private_input_ref: Digest
    code_revision: str
    dependency_lock_digest: Digest
    max_wall_ms: Annotated[int, Field(ge=1, le=900_000)]


class CollaborationSubmission(Contract):
    collaboration_id: Identifier
    bindings_ref: Digest
    supervisor_run_id: Identifier
    supervisor_job_id: Identifier
