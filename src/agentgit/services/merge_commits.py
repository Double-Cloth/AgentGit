"""在调用方批准事务内发布独立合并历史；不创建执行身份或转移授权。"""

import platform
from uuid import UUID, uuid4

from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.merge_proposals import MergeProposalManifest, MergeRevisionBody
from agentgit.core.models import CommitManifest, EnvironmentManifest, EventRange
from agentgit.core.snapshots import (
    ArtifactEntry,
    EventManifest,
    EventPayload,
    LogicalEvent,
    StateSnapshot,
)
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.storage.roles import path_reference_role


def publish_merge_commit(
    history: HistoryService,
    connection: Connection,
    repository: UUID,
    approval_id: UUID,
    proposal_ref: str,
    revision_ref: str,
    ticket_ref: str,
    manifest: MergeProposalManifest,
    candidate: MergeRevisionBody,
    actor: str,
) -> str:
    if candidate.state is None or candidate.conflicts:
        raise ValueError("只有完整且无冲突的候选可发布合并 Commit")
    artifacts = [
        (item, history.objects.put(ObjectType.BLOB, item.text.encode("utf-8")))
        for item in candidate.artifacts
        if item.text is not None
    ]
    state = StateSnapshot(
        data=candidate.state.model_dump(mode="json"),
        artifacts=tuple(
            ArtifactEntry(path=item.path, object_ref=obj.id, media_type=item.media_type)
            for item, obj in sorted(artifacts, key=lambda pair: pair[0].path)
        ),
    )
    state_obj = history.objects.put_json(ObjectType.STATE, state.model_dump(mode="json"))
    frozen = history.objects.put_json(
        ObjectType.BLOB,
        dict(
            schema_version=1,
            kind="merged_input/v1",
            proposal_ref=proposal_ref,
            revision_ref=revision_ref,
            ticket_ref=ticket_ref,
            validator_ref=manifest.request.validator_ref,
            execution="requires_new_run",
        ),
    )
    environment = EnvironmentManifest(
        runtime_version="agentgit-merge/v1",
        python_version=platform.python_version(),
        code_revision=source_revision(),
        dependency_lock_digest=dependency_lock_digest(),
        model_signature="none:merge/v1",
        prompt_digest=object_digest(
            ObjectType.BLOB, encode(candidate.state.goal.model_dump(mode="json"))
        ),
        tool_signatures=(),
        input_ref=frozen.id,
        evaluation_time=candidate.state.constraints.evaluation_time,
        random_seed=0,
    )
    env_obj = history.objects.put_json(ObjectType.ENVIRONMENT, environment.model_dump(mode="json"))
    event = EventPayload(
        data=dict(
            approval_id=str(approval_id),
            proposal_ref=proposal_ref,
            revision_ref=revision_ref,
            ticket_ref=ticket_ref,
            actor=actor,
            execution="requires_new_run",
            candidate_digest=candidate.candidate_digest,
        )
    )
    event_obj = history.objects.put_json(ObjectType.EVENT, event.model_dump(mode="json"))
    events = EventManifest(
        events=(
            LogicalEvent(
                seq=1,
                type="merge.approved",
                logical_step_id="merge",
                payload_ref=event_obj.id,
            ),
        )
    )
    events_obj = history.objects.put_json(ObjectType.EVENT_MANIFEST, events.model_dump(mode="json"))
    commit = CommitManifest(
        repository_id=str(repository),
        parents=(manifest.request.ours.head, manifest.request.theirs.head),
        state_ref=state_obj.id,
        environment_ref=env_obj.id,
        event_manifest_ref=events_obj.id,
        event_range=EventRange(first_seq=1, last_seq=1),
    )
    commit_obj = history.objects.put_json(ObjectType.COMMIT, commit.model_dump(mode="json"))
    objects = [
        state_obj,
        frozen,
        env_obj,
        event_obj,
        events_obj,
        commit_obj,
        *(obj for _, obj in artifacts),
    ]
    for obj in sorted({obj.id: obj for obj in objects}.values(), key=lambda obj: obj.id):
        history._index(connection, obj)
    for role, obj in (
        ("state", state_obj),
        ("environment", env_obj),
        ("input", frozen),
        ("events", events_obj),
        ("merge_event", event_obj),
        ("manifest", commit_obj),
    ):
        history._ref(connection, repository, obj, "commit", commit_obj.id, role)
    connection.execute(
        TABLES["commits"]
        .insert()
        .values(
            id=commit_obj.id,
            repository_id=repository,
            state_ref=state_obj.id,
            environment_ref=env_obj.id,
            event_manifest_ref=events_obj.id,
        )
    )
    for order, parent in enumerate(commit.parents):
        connection.execute(
            TABLES["commit_parents"]
            .insert()
            .values(
                repository_id=repository,
                commit_id=commit_obj.id,
                parent_order=order,
                parent_id=parent,
            )
        )
    for item, obj in artifacts:
        history._ref(
            connection,
            repository,
            obj,
            "commit",
            commit_obj.id,
            path_reference_role("artifact", item.path),
        )
        connection.execute(
            TABLES["artifacts"]
            .insert()
            .values(
                id=uuid4(),
                repository_id=repository,
                commit_id=commit_obj.id,
                relative_path=item.path,
                object_id=obj.id,
                media_type=item.media_type,
            )
        )
    history.fault("merge.commit_stored")
    return commit_obj.id
