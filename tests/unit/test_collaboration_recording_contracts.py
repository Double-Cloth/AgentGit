"""来源索引的完整范围、身份和有界契约。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.adapters.tools import (
    ReadArguments,
    SearchArguments,
    WriteArguments,
    local_tool_signature,
)
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration import CollaborationRole
from agentgit.core.collaboration_recordings import (
    CollaborationRoleSource,
    CollaborationSourceFrame,
    CollaborationSourceIndex,
)


def sample():
    frame = CollaborationSourceFrame(
        commit_id="1" * 64,
        state_ref="2" * 64,
        environment_ref="3" * 64,
        event_manifest_ref="4" * 64,
        first_seq=1,
        last_seq=2,
    )
    return CollaborationSourceIndex(
        repository_id=str(uuid4()),
        collaboration_id=str(uuid4()),
        program_ref="1" * 64,
        bindings_ref="2" * 64,
        environment_ref="3" * 64,
        input_ref="4" * 64,
        corpus_ref="5" * 64,
        code_revision="source-sha256:" + "6" * 64,
        dependency_lock_digest="7" * 64,
        roles=tuple(
            CollaborationRoleSource(
                role=r,
                task_id=str(uuid4()),
                run_id=str(uuid4()),
                job_id=str(uuid4()),
                payload_ref="1" * 64,
                policy_ref="2" * 64,
                budget_ref="3" * 64,
                private_input_ref="4" * 64,
                frames=(frame,),
                call_ids=(),
            )
            for r in CollaborationRole
        ),
        message_ids=tuple(str(uuid4()) for _ in range(7)),
        shared_head_refs=tuple(str(n) * 64 for n in range(1, 6)),
        object_refs=("1" * 64,),
        total_bytes=1,
    )


@pytest.mark.parametrize(
    "fault",
    [
        "partial",
        "role",
        "run",
        "gap",
        "frame",
        "messages",
        "heads",
        "objects",
        "bytes",
        "events",
        "extra",
    ],
)
def test_source_index_rejects_ambiguous_incomplete_or_unbounded_scope(fault):
    value = sample().model_dump(mode="json")
    if fault == "partial":
        value["roles"].pop()
    elif fault == "role":
        value["roles"][1]["role"] = "supervisor"
    elif fault == "run":
        value["roles"][1]["run_id"] = value["roles"][0]["run_id"]
    elif fault == "gap":
        value["roles"][0]["frames"][0]["first_seq"] = 2
    elif fault == "frame":
        value["roles"][0]["frames"] *= 2
    elif fault == "messages":
        value["message_ids"][1] = value["message_ids"][0]
    elif fault == "heads":
        value["shared_head_refs"][1] = value["shared_head_refs"][0]
    elif fault == "objects":
        value["object_refs"] *= 2
    elif fault == "bytes":
        value["total_bytes"] = 64 * 1024 * 1024 + 1
    elif fault == "events":
        value["roles"][0]["frames"][0]["last_seq"] = 50001
    else:
        value["fallback_live"] = True
    with pytest.raises(ValidationError):
        CollaborationSourceIndex.model_validate_json(encode(value))


@pytest.mark.parametrize(
    "name,schema,effect",
    [
        ("corpus_search", SearchArguments, "READ_ONLY"),
        ("source_read", ReadArguments, "READ_ONLY"),
        ("artifact_write", WriteArguments, "IDEMPOTENT_WRITE"),
    ],
)
def test_pure_signature_preserves_existing_local_tool_contract(name, schema, effect):
    expected = object_digest(
        ObjectType.CALL,
        encode(
            dict(
                schema_version=1,
                tool=name,
                version="v1",
                effect=effect,
                arguments=schema.model_json_schema(),
            )
        ),
    )
    assert local_tool_signature(name) == expected
