"""核验 Recorded 的耐久证明并追溯原始录制，不将快照当成执行结果。"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.jobs import JobPayload
from agentgit.core.models import CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.snapshots import EventManifest, EventPayload, LogicalEvent, StateSnapshot
from agentgit.core.versions import RecordedRange
from agentgit.services.history import TABLES
from agentgit.services.versions import HistorySelection

if TYPE_CHECKING:
    from agentgit.services.recordings import RecordedBundle, RecordingReader


class ProjectionReader:
    def __init__(self, reader: "RecordingReader"):
        self.reader, self.history = reader, reader.history

    def load(
        self,
        repository: UUID,
        selection: HistorySelection,
        environment: EnvironmentManifest,
        seen: tuple[UUID, ...],
        account: Callable[[bytes], bytes],
    ) -> "RecordedBundle":
        from agentgit.services.recordings import RecordedBundle, RecordedProjection

        jobs, points = TABLES["jobs"], TABLES["replay_checkpoints"]
        with self.history.engine.connect() as connection:
            family = self.reader.versions._family(connection, repository, selection.source_run_id)
            candidates = list(
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository,
                        sa.or_(jobs.c.run_id.in_(family), jobs.c.origin_run_id.in_(family)),
                    )
                ).mappings()
            )
            positions = {run: index for index, run in enumerate(family)}

            def distance(row: Any) -> int:
                return min(
                    positions.get(row["run_id"], 100), positions.get(row["origin_run_id"], 100)
                )

            candidates.sort(key=distance)
            if not candidates or (
                len(candidates) > 1 and distance(candidates[0]) == distance(candidates[1])
            ):
                raise AgentGitError(ErrorCode.RECORDING_MISSING, "Recorded 来源没有唯一冻结 Job")
            job = candidates[0]
            root = job["origin_run_id"] or job["run_id"]
            if root not in positions:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 来源未到达自身 Job 根")
            allowed = set(family[: positions[root] + 1])
            root_run = dict(self.history._run(connection, repository, root))
            request = (
                connection.execute(
                    sa.select(TABLES["replay_requests"]).where(
                        TABLES["replay_requests"].c.repository_id == repository,
                        TABLES["replay_requests"].c.job_id == job["id"],
                    )
                )
                .mappings()
                .first()
            )
            proofs = list(
                connection.execute(
                    sa.select(points)
                    .where(
                        points.c.repository_id == repository,
                        points.c.job_id == job["id"],
                    )
                    .order_by(points.c.source_order)
                    .limit(10002)
                ).mappings()
            )
        payload = parse_contract(
            account(self.history.objects.get(job["payload_ref"], ObjectType.BLOB)), JobPayload
        )
        spec = payload.replay
        if (
            spec is None
            or spec.mode != "recorded"
            or request is None
            or any(
                request[key] != value
                for key, value in (
                    ("mode", "recorded"),
                    ("source_run_id", UUID(spec.source_run_id)),
                    ("from_commit_id", spec.from_commit),
                    ("until_commit_id", spec.until_commit),
                    ("environment_ref", spec.environment_ref),
                )
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 来源请求与冻结 Job 不符")
        if (
            positions[root] + 1 >= len(family)
            or family[positions[root] + 1] != UUID(spec.source_run_id)
            or root_run["mode"] != "recorded"
            or root_run["base_commit_id"] != spec.from_commit
            or str(root_run["agent_config_id"]) != payload.config_id
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded Job 根与原来源或配置不符")
        if (
            len(proofs) > 10001
            or len(proofs) > spec.checkpoint_count
            or any(row["source_order"] != index for index, row in enumerate(proofs))
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 来源证明顺序不完整")
        by_commit = {row["commit_id"]: row for row in proofs}
        if len(by_commit) != len(proofs):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 来源证明重复目标检查点")
        checkpoints: list[tuple[str, StateSnapshot]] = []
        origins: list[str] = []
        events: list[tuple[str, dict[str, Any]] | None] = []
        previous_order: int | None = None
        event_count = 0
        for commit in selection.commits:
            view = self.reader.versions.checkout(repository, commit)
            account(encode(view))
            found = parse_contract(encode(view["environment"]), EnvironmentManifest)
            if found != environment:
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "复合录制的冻结环境不兼容")
            manifest = parse_contract(encode(view["manifest"]), CommitManifest)
            checkpoints.append((commit, parse_contract(encode(view["snapshot"]), StateSnapshot)))
            if commit == spec.from_commit:
                order, origin, logical = -1, spec.from_commit, None
            else:
                proof = by_commit.get(commit)
                if proof is None:
                    raise AgentGitError(ErrorCode.RECORDING_MISSING, "选定检查点缺少 Recorded 证明")
                if proof["run_id"] not in allowed or proof["state_ref"] != manifest.state_ref:
                    raise AgentGitError(
                        ErrorCode.OBJECT_CORRUPT, "Recorded 证明不属于来源 Job 或状态"
                    )
                order, origin = proof["source_order"], proof["source_commit_id"]
                parent = spec.from_commit if order == 0 else proofs[order - 1]["commit_id"]
                if (
                    manifest.parents != (parent,)
                    or manifest.environment_ref != spec.environment_ref
                ):
                    raise AgentGitError(
                        ErrorCode.REPLAY_MISMATCH, "Recorded 证明的父关系或环境不符"
                    )
                source = self.reader.versions.checkout(repository, origin)
                account(encode(source))
                if (
                    source["manifest"]["state_ref"] != manifest.state_ref
                    or source["environment"] != view["environment"]
                ):
                    raise AgentGitError(
                        ErrorCode.REPLAY_MISMATCH, "Recorded 证明与原状态或环境不符"
                    )
                journal = parse_contract(
                    account(
                        self.history.objects.get(
                            manifest.event_manifest_ref, ObjectType.EVENT_MANIFEST
                        )
                    ),
                    EventManifest,
                )
                event_count += len(journal.events)
                if event_count > 50000 or not journal.events:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Recorded 证明事件超过有界上限")
                with self.history.engine.connect() as connection:
                    rows = list(
                        connection.execute(
                            sa.select(TABLES["events"])
                            .where(
                                TABLES["events"].c.repository_id == repository,
                                TABLES["events"].c.run_id == proof["run_id"],
                                TABLES["events"].c.seq >= manifest.event_range.first_seq,
                                TABLES["events"].c.seq <= manifest.event_range.last_seq,
                            )
                            .order_by(TABLES["events"].c.seq)
                            .limit(50001)
                        ).mappings()
                    )
                indexed = tuple(
                    LogicalEvent(**self.history._logical_event(row, proof["run_id"]))
                    for row in rows
                )
                if indexed != journal.events:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Recorded 证明事件索引与清单不符")
                event = journal.events[-1]
                data = parse_contract(
                    account(self.history.objects.get(event.payload_ref, ObjectType.EVENT)),
                    EventPayload,
                ).data
                if (
                    event.type != ("replay.restored" if order == 0 else "replay.checkpoint")
                    or set(data)
                    != {"source_commit", "source_order", "source_event_type", "source_details"}
                    or type(data["source_order"]) is not int
                    or data["source_order"] != order
                    or data["source_commit"] != origin
                    or not isinstance(data["source_event_type"], str)
                    or not isinstance(data["source_details"], dict)
                    or (
                        order == 0
                        and (
                            origin != spec.from_commit
                            or data["source_event_type"] != "replay.restored"
                            or data["source_details"] != {}
                        )
                    )
                ):
                    raise AgentGitError(
                        ErrorCode.REPLAY_MISMATCH, "Recorded 证明事件与来源绑定不符"
                    )
                logical = (data["source_event_type"], data["source_details"])
            if previous_order is not None and order != previous_order + 1:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "复合录制范围跳过来源证明")
            previous_order = order
            origins.append(origin)
            events.append(logical)
        origin_bundle = self.reader._read(
            repository,
            RecordedRange(
                source_run_id=spec.source_run_id, from_commit=origins[0], until_commit=origins[-1]
            ),
            environment,
            seen,
            account,
        )
        compressed = tuple(
            commit
            for index, commit in enumerate(origins)
            if index == 0 or commit != origins[index - 1]
        )
        if compressed != origin_bundle.selection.commits:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "复合证明没有连续覆盖原始检查点路径")
        return RecordedBundle(
            selection,
            environment,
            tuple(checkpoints),
            (),
            projection=RecordedProjection(origin_bundle, tuple(origins), tuple(events)),
        )
