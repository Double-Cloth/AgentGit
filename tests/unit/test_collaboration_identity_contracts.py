"""Recorded 身份映射只改变类型化身份；私有引用/文本与调用不变。"""

from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationBindings,
    CollaborationMessage,
    CollaborationRole,
    RoleBinding,
    SharedArtifactHead,
)
from agentgit.core.collaboration_execution import CollaborationRoleState
from agentgit.core.collaboration_identities import CollaborationIdentityMap, RoleJobIdentity
from agentgit.core.collaboration_runs import CollaborationFrozenInput, CollaborationRolePayload
from agentgit.core.errors import AgentGitError
from agentgit.core.research import evolve


def uid(n):
    return str(UUID(int=n))


def mapping():
    def bindings(start):
        return CollaborationBindings(
            repository_id=uid(1),
            collaboration_id=uid(start),
            agents=tuple(
                RoleBinding(role=r, agent_run_id=uid(start + 10 + i), task_id=uid(start + 20 + i))
                for i, r in enumerate(CollaborationRole)
            ),
        )

    return CollaborationIdentityMap(
        source=bindings(100),
        target=bindings(200),
        jobs=tuple(
            RoleJobIdentity(role=r, source_job_id=uid(300 + i), target_job_id=uid(400 + i))
            for i, r in enumerate(CollaborationRole)
        ),
    )


@pytest.mark.parametrize(
    "fault", ["repository", "group", "run", "task", "job", "duplicate", "role"]
)
def test_identity_map_rejects_reused_cross_project_or_ambiguous_identities(fault):
    data = mapping().model_dump(mode="json")
    if fault == "repository":
        data["target"]["repository_id"] = uid(2)
    elif fault == "group":
        data["target"]["collaboration_id"] = data["source"]["collaboration_id"]
    elif fault in ("run", "task"):
        field = "agent_run_id" if fault == "run" else "task_id"
        data["target"]["agents"][0][field] = data["source"]["agents"][1][field]
    elif fault == "job":
        data["jobs"][0]["target_job_id"] = data["jobs"][1]["source_job_id"]
    elif fault == "duplicate":
        data["jobs"][0]["target_job_id"] = data["jobs"][1]["target_job_id"]
    else:
        data["jobs"][0]["role"] = "writer"
    with pytest.raises(ValidationError):
        CollaborationIdentityMap.model_validate_json(encode(data))


def test_typed_state_and_transport_mapping_preserve_private_and_content_refs():
    identities = mapping()
    old = identities.source.agents[1]
    source_parent = CausalEventAddress(
        agent_run_id=old.agent_run_id, commit_id="1" * 64, seq=7, payload_ref="2" * 64
    )
    target_parent = evolve(
        source_parent,
        agent_run_id=identities.run(old.agent_run_id),
        commit_id="3" * 64,
        seq=9,
        payload_ref="4" * 64,
    )
    message_id = str(uuid5(UUID(identities.source.collaboration_id), "researcher:evidence:writer"))
    source_head = SharedArtifactHead(
        repository_id=uid(1),
        collaboration_id=identities.source.collaboration_id,
        program_ref="5" * 64,
        bindings_ref=object_digest(
            ObjectType.BLOB, encode(identities.source.model_dump(mode="json"))
        ),
        path="evidence.json",
        version=1,
        object_ref="6" * 64,
        publisher_event=source_parent,
    )
    state = CollaborationRoleState(
        collaboration_id=identities.source.collaboration_id,
        role=old.role,
        program_ref="5" * 64,
        private_input_ref="7" * 64,
        sent=(message_id,),
        publications=(source_head,),
        source_ids=(old.agent_run_id,),
        error_code=old.agent_run_id,
        exports={"evidence.json": "6" * 64},
    )
    changed = identities.state(state, {source_parent: target_parent})
    assert changed.source_ids == state.source_ids and changed.error_code == state.error_code
    assert changed.private_input_ref == state.private_input_ref and changed.exports == state.exports
    assert (
        changed.sent != state.sent
        and changed.collaboration_id == identities.target.collaboration_id
    )
    assert changed.publications[0].publisher_event == target_parent
    assert changed.publications[0].bindings_ref == object_digest(
        ObjectType.BLOB, encode(identities.target.model_dump(mode="json"))
    )
    target = identities.source.agents[3]
    envelope = CollaborationMessage(
        repository_id=uid(1),
        collaboration_id=identities.source.collaboration_id,
        message_id=message_id,
        sender=old.agent_run_id,
        receiver=target.agent_run_id,
        task_id=target.task_id,
        correlation_id=identities.source.collaboration_id,
        kind="evidence",
        payload_ref="6" * 64,
        parent_event_id=source_parent,
    )
    mapped = identities.message(envelope, target_parent)
    assert mapped.payload_ref == envelope.payload_ref and mapped.message_id == changed.sent[0]
    assert mapped.receiver == identities.run(target.agent_run_id)
    with pytest.raises(AgentGitError):
        identities.state(state, {})
    with pytest.raises(AgentGitError):
        identities.message(envelope, source_parent)
    with pytest.raises(AgentGitError):
        identities.message_id(uid(999))


@pytest.mark.parametrize("field", ["recording_ref", "source_run_id"])
def test_recorded_role_payload_requires_both_source_fields(field):
    fields = dict(
        collaboration_id=uid(1),
        role="researcher",
        program_ref="1" * 64,
        bindings_ref="2" * 64,
        environment_ref="3" * 64,
        policy_ref="4" * 64,
        private_input_ref="5" * 64,
        code_revision="source",
        dependency_lock_digest="6" * 64,
        max_wall_ms=1000,
    )
    live = CollaborationRolePayload.model_validate_json(encode(dict(schema_version=1, **fields)))
    assert "recording_ref" not in live.model_dump(
        mode="json"
    ) and "source_run_id" not in live.model_dump(mode="json")
    fields[field] = "7" * 64 if field == "recording_ref" else uid(2)
    with pytest.raises(ValidationError):
        CollaborationRolePayload.model_validate_json(encode(dict(schema_version=1, **fields)))


def test_live_frozen_input_keeps_original_canonical_fields():
    data = dict(
        schema_version=1,
        kind="collaboration-frozen-input/v1",
        program_ref="1" * 64,
        bindings_ref="2" * 64,
        corpus_ref="3" * 64,
        corpus_digest="4" * 64,
    )
    assert (
        CollaborationFrozenInput.model_validate_json(encode(data)).model_dump(mode="json") == data
    )
