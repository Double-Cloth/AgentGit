"""后端核验整组后只返回租约所属角色的重算数据，不向角色传递其他私有字节。"""

from dataclasses import dataclass

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration_execution import CollaborationRoleState
from agentgit.core.collaboration_identities import CollaborationIdentityMap
from agentgit.core.collaboration_recordings import CollaborationRoleSource
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import Lease
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.snapshots import EventManifest
from agentgit.replay.collaboration import CollaborationRecordedEngine
from agentgit.replay.recorded import Recording
from agentgit.services.collaboration import CollaborationService
from agentgit.services.collaboration_recordings import CollaborationRecordingReader
from agentgit.services.history import TABLES
from agentgit.services.jobs import JobService


@dataclass(frozen=True)
class RecordedRoleLane:
    mapping: CollaborationIdentityMap
    source: CollaborationRoleSource
    states: tuple[CollaborationRoleState, ...]
    recordings: tuple[Recording, ...]
    event_types: tuple[str, ...]
    environment: EnvironmentManifest


def verified_role_lane(queue: JobService, lease: Lease) -> RecordedRoleLane:
    service = CollaborationService(queue)
    scope = service.role_input(lease)
    with queue.history.engine.connect() as c:
        link = (
            c.execute(
                sa.select(TABLES["collaboration_recordings"]).where(
                    TABLES["collaboration_recordings"].c.repository_id == lease.repository_id,
                    TABLES["collaboration_recordings"].c.collaboration_id
                    == scope.bindings.collaboration_id,
                )
            )
            .mappings()
            .one()
        )
        group = (
            c.execute(
                sa.select(TABLES["collaborations"]).where(
                    TABLES["collaborations"].c.repository_id == lease.repository_id,
                    TABLES["collaborations"].c.id == scope.bindings.collaboration_id,
                )
            )
            .mappings()
            .one()
        )
        mapping = parse_contract(
            service._owned(
                c,
                lease.repository_id,
                link["identity_map_ref"],
                "collaboration_recording",
                scope.bindings.collaboration_id,
                "identities",
            ),
            CollaborationIdentityMap,
        )
    # 这是由已授权准备所委托的后端审计，不是子角色的模型上下文或 CAS 读取接口。
    source = CollaborationRecordingReader(queue.history).inspect(
        lease.repository_id, link["source_collaboration_id"], actor=group["actor"]
    )
    if (
        object_digest(ObjectType.BLOB, encode(source.index.model_dump(mode="json")))
        != link["source_index_ref"]
    ):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "当前完整来源与冻结索引不符")
    result = CollaborationRecordedEngine(source).replay()
    index = next(i for i, r in enumerate(source.index.roles) if r.role == scope.policy.role)
    role = source.index.roles[index]
    raw = {ref: data for ref, _, data in source.objects}
    return RecordedRoleLane(
        mapping,
        role,
        result.states[index],
        source.recordings[index],
        tuple(
            parse_contract(raw[f.event_manifest_ref], EventManifest).events[-1].type
            for f in role.frames
        ),
        parse_contract(raw[role.frames[0].environment_ref], EnvironmentManifest),
    )
