"""来源路径、安全 Fork、只读版本浏览与客观差异。"""

import heapq
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.execution import SANDBOX_PROVIDERS, StoppedExecution
from agentgit.core.models import CallIntent, CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentState
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import NewBranch, RecordedRange
from agentgit.diff.structural import snapshot_changes
from agentgit.services.history import TABLES, HistoryService
from agentgit.storage.database import transaction

MAX_HISTORY = 10000


@dataclass(frozen=True)
class HistorySelection:
    source_run_id: UUID
    commits: tuple[str, ...]
    from_seq: int
    until_seq: int


@dataclass(frozen=True)
class PreparedBranch:
    repository_id: UUID
    request: NewBranch
    snapshot_payload: bytes
    ancestors: frozenset[str]


class VersionService:
    def __init__(self, history: HistoryService):
        self.history = history

    @staticmethod
    def _page(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentGitError(ErrorCode.VALIDATION, "分页大小必须为 1–1000")

    @staticmethod
    def _digest(value: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise AgentGitError(ErrorCode.VALIDATION, "Commit 标识必须为完整内容摘要")

    def _manifest(self, repository_id: UUID, row: dict[str, Any]) -> CommitManifest:
        manifest = parse_contract(
            self.history.objects.get(row["id"], ObjectType.COMMIT), CommitManifest
        )
        expected_parent = row["parents"]
        if (
            manifest.repository_id != str(repository_id)
            or manifest.parents != expected_parent
            or any(
                getattr(manifest, key) != row[key]
                for key in ("state_ref", "environment_ref", "event_manifest_ref")
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "历史拓扑或 Commit 索引与内容不符")
        return manifest

    def ancestors(self, repository_id: UUID, start: str) -> list[str]:
        self._digest(start)
        # 分批读取唯一节点，避免双父菱形的路径乘积；父必须位于全部子节点之后。
        graph: dict[str, tuple[str, ...]] = {}
        pending = [start]
        with self.history.engine.connect() as connection:
            while pending:
                batch = list(dict.fromkeys(node for node in pending if node not in graph))
                pending = []
                if len(graph) + len(batch) > MAX_HISTORY:
                    raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "历史超过有界唯一节点上限")
                commits, parents = TABLES["commits"], TABLES["commit_parents"]
                rows = {
                    row["id"]: dict(row)
                    for row in connection.execute(
                        sa.select(commits).where(
                            commits.c.repository_id == repository_id,
                            commits.c.id.in_(batch),
                        )
                    ).mappings()
                }
                if len(rows) != len(batch):
                    raise AgentGitError(ErrorCode.NOT_FOUND, "Commit 不可访问或父历史缺失")
                indexed: dict[str, list[str]] = {node: [] for node in batch}
                for row in connection.execute(
                    sa.select(parents)
                    .where(
                        parents.c.repository_id == repository_id,
                        parents.c.commit_id.in_(batch),
                    )
                    .order_by(parents.c.parent_order)
                ).mappings():
                    indexed_values = indexed[row["commit_id"]]
                    if (
                        row["parent_order"] != len(indexed_values)
                        or len(indexed_values) >= 2
                        or row["parent_id"] in indexed_values
                    ):
                        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "父边顺序或身份非法")
                    indexed_values.append(row["parent_id"])
                for node in batch:
                    values = tuple(indexed[node])
                    rows[node]["parents"] = values
                    self._manifest(repository_id, rows[node])
                    graph[node] = values
                    pending.extend(values)
        rank = {node: index for index, node in enumerate(graph)}
        children = dict.fromkeys(graph, 0)
        for values in graph.values():
            for node in values:
                children[node] += 1
        ready = [rank[node] for node, count in children.items() if count == 0]
        heapq.heapify(ready)
        nodes = list(graph)
        result = []
        while ready:
            node = nodes[heapq.heappop(ready)]
            result.append(node)
            for parent in graph[node]:
                children[parent] -= 1
                if children[parent] == 0:
                    heapq.heappush(ready, rank[parent])
        if len(result) != len(graph):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "历史包含循环")
        return result

    def checkout(self, repository_id: UUID, commit_id: str) -> dict[str, Any]:
        self._digest(commit_id)
        snapshot = self.history.restore(repository_id, commit_id)
        manifest = parse_contract(
            self.history.objects.get(commit_id, ObjectType.COMMIT), CommitManifest
        )
        environment = parse_contract(
            self.history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT),
            EnvironmentManifest,
        )
        return {
            "commit_id": commit_id,
            "manifest": manifest.model_dump(mode="json"),
            "environment": environment.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
        }

    def select_range(self, repository_id: UUID, request: RecordedRange) -> HistorySelection:
        if (
            self.history.restore(repository_id, request.from_commit).data.get("kind")
            == "merged_research_state/v1"
        ):
            raise AgentGitError(
                ErrorCode.RECORDING_MISSING,
                "合并 Commit 没有录制路径，请显式选择新 Run 的初始化检查点",
            )
        source = UUID(request.source_run_id)
        links, snapshots = TABLES["run_commits"], TABLES["snapshots"]
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repository_id, source)
            rows = list(
                connection.execute(
                    sa.select(links.c.commit_id, links.c.run_order, snapshots.c.event_seq)
                    .join(
                        snapshots,
                        sa.and_(
                            snapshots.c.run_id == links.c.run_id,
                            snapshots.c.commit_id == links.c.commit_id,
                            snapshots.c.repository_id == links.c.repository_id,
                        ),
                    )
                    .where(links.c.repository_id == repository_id, links.c.run_id == source)
                    .order_by(links.c.run_order)
                    .limit(MAX_HISTORY + 1)
                ).mappings()
            )
        if len(rows) > MAX_HISTORY:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "来源 Run 超过有界历史上限")
        if any(row["run_order"] != index for index, row in enumerate(rows, 1)):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源 Run 的检查点顺序不完整")
        path = ([] if run["base_commit_id"] is None else [run["base_commit_id"]]) + [
            row["commit_id"] for row in rows
        ]
        if len(set(path)) != len(path) or (path and path[-1] != run["checkpoint_commit_id"]):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源 Run 的 HEAD 或路径索引不符")
        if request.from_commit not in path or request.until_commit not in path:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "起止点不属于同一来源 Run 路径")
        first, last = path.index(request.from_commit), path.index(request.until_commit)
        if first > last:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "回放终点不能早于起点")
        selected = tuple(path[first : last + 1])
        # 显式 Run 路径逐边核对，不将 DAG 的拓扑排序当作唯一录制路径。
        self.ancestors(repository_id, request.until_commit)
        for previous, current in zip(selected[:-1], selected[1:], strict=True):
            manifest = parse_contract(
                self.history.objects.get(current, ObjectType.COMMIT), CommitManifest
            )
            if manifest.parents != (previous,):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "起止点之间不存在唯一来源录制路径")
        sequences = {row["commit_id"]: row["event_seq"] for row in rows}
        if run["base_commit_id"] is not None:
            sequences[run["base_commit_id"]] = 0
        return HistorySelection(
            source, selected, sequences[request.from_commit], sequences[request.until_commit]
        )

    def _family(self, connection: Connection, repository_id: UUID, source: UUID) -> list[UUID]:
        family: list[UUID] = []
        current: UUID | None = source
        while current is not None:
            if current in family or len(family) >= 100:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "来源 Run 链包含循环或过长")
            family.append(current)
            current = self.history._run(connection, repository_id, current)["origin_run_id"]
        return family

    def _safe(
        self,
        connection: Connection,
        repository_id: UUID,
        source: UUID,
        commit_id: str,
        ancestors: set[str],
        snapshot: StateSnapshot,
    ) -> None:
        family = self._family(connection, repository_id, source)
        for run_id in sorted(family):
            self.history._run(connection, repository_id, run_id, lock=True)
        run = self.history._run(connection, repository_id, source)
        if snapshot.data.get("status") in (
            "outcome_unknown",
            "waiting_approval",
            "interrupted",
        ) or (run["status"] == "outcome_unknown" and run["checkpoint_commit_id"] == commit_id):
            raise AgentGitError(ErrorCode.FORBIDDEN, "只能从安全检查点 Fork")
        stopped: tuple[StoppedExecution, ...] = ()
        if snapshot.data.get("stopped_executions"):
            stopped = parse_contract(encode(snapshot.data), AgentState).stopped_executions
        calls = TABLES["external_calls"]
        pending = list(
            connection.execute(
                sa.select(calls)
                .where(
                    calls.c.repository_id == repository_id,
                    calls.c.run_id.in_(family),
                    calls.c.effect.in_(("NON_IDEMPOTENT_WRITE", "IRREVERSIBLE")),
                    calls.c.status.in_(("proposed", "started", "outcome_unknown")),
                    calls.c.result_ref.is_(None),
                )
                .limit(1001)
            ).mappings()
        )
        if len(pending) > 1000:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "未决效果超过有界检查上限")
        for call in pending:
            intent = parse_contract(
                self.history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent
            )
            if (
                intent.effect.value != call["effect"]
                or intent.request_ref != call["request_digest"]
                or any(
                    getattr(intent.occurrence, key) != call[key]
                    for key in (
                        "logical_step_id",
                        "call_index",
                        "attempt",
                        "provider",
                        "signature_digest",
                        "request_digest",
                    )
                )
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "未决调用索引与原意图不符")
            if intent.checkpoint not in ancestors:
                continue
            candidates = [item for item in stopped if item.occurrence == intent.occurrence]
            if call["provider"] not in SANDBOX_PROVIDERS or len(candidates) != 1:
                raise AgentGitError(ErrorCode.FORBIDDEN, "检查点仍有未核对的写入动作")
            executions, refs = TABLES["sandbox_executions"], TABLES["object_refs"]
            execution = (
                connection.execute(
                    sa.select(executions).where(
                        executions.c.repository_id == repository_id,
                        executions.c.call_id == call["id"],
                        executions.c.cleanup_confirmed.is_(True),
                        executions.c.policy_ref == candidates[0].execution_policy_ref,
                        executions.c.input_manifest_ref == candidates[0].input_manifest_ref,
                    )
                )
                .mappings()
                .first()
            )
            proofs = list(
                connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == repository_id,
                        refs.c.owner_type == "sandbox_execution",
                        refs.c.owner_id == str(call["id"]),
                        refs.c.role == "recovery_cleanup",
                    )
                ).scalars()
            )
            if execution is None or len(proofs) != 1:
                raise AgentGitError(ErrorCode.FORBIDDEN, "停止执行缺少可信清理依据")
            raw = self.history.objects.get(proofs[0], ObjectType.BLOB)
            proof = decode(raw)
            if (
                len(raw) > 4096
                or not isinstance(proof, dict)
                or set(proof)
                != {
                    "schema_version",
                    "container_name",
                    "container_id",
                    "cleanup_confirmed",
                    "previous_fencing_token",
                    "observer_fencing_token",
                }
                or type(proof.get("schema_version")) is not int
                or proof.get("schema_version") != 1
                or proof.get("cleanup_confirmed") is not True
                or proof.get("container_name") != execution["container_name"]
                or proof.get("container_id") != execution["container_id"]
                or proof.get("previous_fencing_token") != execution["fencing_token"]
                or type(proof.get("previous_fencing_token")) is not int
                or type(proof.get("observer_fencing_token")) is not int
                or proof["observer_fencing_token"] <= execution["fencing_token"]
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "清理证明与原执行身份不符")

    def fork(
        self,
        repository_id: UUID,
        payload: NewBranch,
        *,
        actor: str,
        connection: Connection | None = None,
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "Fork 必须记录有界操作者")
        prepared = self.prepare_fork(repository_id, payload)
        with transaction(self.history.engine, connection) as connection:
            return self.publish_fork(prepared, actor=actor, connection=connection)

    def prepare_fork(self, repository_id: UUID, payload: NewBranch) -> PreparedBranch:
        payload = parse_contract(encode(payload.model_dump(mode="json")), NewBranch)
        self.select_range(
            repository_id,
            RecordedRange(
                source_run_id=payload.source_run_id,
                from_commit=payload.from_commit,
                until_commit=payload.from_commit,
            ),
        )
        snapshot = self.history.restore(repository_id, payload.from_commit)
        ancestors = frozenset(self.ancestors(repository_id, payload.from_commit))
        return PreparedBranch(
            repository_id, payload, encode(snapshot.model_dump(mode="json")), ancestors
        )

    def publish_fork(
        self,
        prepared: PreparedBranch,
        *,
        actor: str,
        connection: Connection,
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "Fork 必须记录有界操作者")
        repository_id, payload = prepared.repository_id, prepared.request
        snapshot = parse_contract(prepared.snapshot_payload, StateSnapshot)
        with transaction(self.history.engine, connection) as connection:
            self._safe(
                connection,
                repository_id,
                UUID(payload.source_run_id),
                payload.from_commit,
                set(prepared.ancestors),
                snapshot,
            )
            branches = TABLES["branches"]
            inserted = connection.execute(
                insert(branches)
                .values(
                    repository_id=repository_id,
                    name=payload.name,
                    head_commit_id=payload.from_commit,
                    version=0,
                )
                .on_conflict_do_nothing()
                .returning(branches.c.name)
            ).first()
            if inserted is None:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "分支名已存在")
            connection.execute(
                TABLES["branch_forks"]
                .insert()
                .values(
                    repository_id=repository_id,
                    branch_name=payload.name,
                    source_run_id=UUID(payload.source_run_id),
                    from_commit_id=payload.from_commit,
                    actor=actor,
                )
            )
        return {
            "repository_id": str(repository_id),
            "name": payload.name,
            "version": 0,
            "head_commit_id": payload.from_commit,
            "source_run_id": payload.source_run_id,
        }

    def branches(
        self,
        repository_id: UUID,
        *,
        after_name: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._page(limit)
        branches, forks = TABLES["branches"], TABLES["branch_forks"]
        with self.history.engine.connect() as connection:
            if (
                connection.execute(
                    sa.select(TABLES["repositories"].c.id).where(
                        TABLES["repositories"].c.id == repository_id
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
            query = (
                sa.select(branches, forks.c.source_run_id, forks.c.from_commit_id)
                .outerjoin(
                    forks,
                    sa.and_(
                        forks.c.repository_id == branches.c.repository_id,
                        forks.c.branch_name == branches.c.name,
                    ),
                )
                .where(branches.c.repository_id == repository_id)
            )
            if after_name is not None:
                query = query.where(branches.c.name > after_name)
            rows = [
                dict(row)
                for row in connection.execute(
                    query.order_by(branches.c.name).limit(limit + 1)
                ).mappings()
            ]
        items = rows[:limit]
        if items:
            origins = TABLES["merge_branch_origins"]
            with self.history.engine.connect() as connection:
                merged = {
                    item["branch_name"]: item
                    for item in connection.execute(
                        sa.select(origins).where(
                            origins.c.repository_id == repository_id,
                            origins.c.branch_name.in_([item["name"] for item in items]),
                        )
                    ).mappings()
                }
            for item in items:
                if item["name"] in merged:
                    origin = merged[item["name"]]
                    item.update(
                        origin_kind="merge",
                        merge_approval_id=str(origin["approval_id"]),
                        merge_commit_id=origin["commit_id"],
                    )
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_after_name": items[-1]["name"] if items else after_name,
        }

    def log(
        self,
        repository_id: UUID,
        *,
        branch_name: str = "main",
        after_commit: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._page(limit)
        head = self.history.branch(repository_id, branch_name)["head_commit_id"]
        chain = [] if head is None else self.ancestors(repository_id, head)
        if after_commit is not None:
            self._digest(after_commit)
            if after_commit not in chain:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "历史游标不在当前分支路径")
            chain = chain[chain.index(after_commit) + 1 :]
        items = []
        for commit_id in chain[:limit]:
            manifest = parse_contract(
                self.history.objects.get(commit_id, ObjectType.COMMIT), CommitManifest
            )
            items.append({"commit_id": commit_id, **manifest.model_dump(mode="json")})
        return {
            "items": items,
            "has_more": len(chain) > limit,
            "next_after_commit": items[-1]["commit_id"] if items else after_commit,
        }

    def diff(
        self,
        repository_id: UUID,
        left: str,
        right: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        self._page(limit)
        if type(after) is not int or not 0 <= after <= 9007199254740991:
            raise AgentGitError(ErrorCode.VALIDATION, "差异游标必须为非负整数")
        from agentgit.services.merge import MergePreviewService

        # 多个最佳共同祖先只返回集合，不凭拓扑展示顺序任选一个。
        selection = MergePreviewService(self.history).bases(repository_id, left, right)
        common = selection.selected
        changes = snapshot_changes(
            self.history.restore(repository_id, left), self.history.restore(repository_id, right)
        )
        return {
            "left_commit": left,
            "right_commit": right,
            "common_ancestor": common,
            "common_ancestors": list(selection.bases),
            "merge_base_status": selection.status,
            "items": [item.model_dump(mode="json") for item in changes[after : after + limit]],
            "total_changes": len(changes),
            "next_after": after + len(changes[after : after + limit]),
            "has_more": after + limit < len(changes),
        }
