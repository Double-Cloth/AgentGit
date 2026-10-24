"""事务外核验已发布补丁和失败条件，事务内冻结搜索及全部提案。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.debug_reports import NewDebugReport
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.models import parse_contract
from agentgit.core.repair import (
    RepairBudget,
    RepairProposal,
    RepairSearchManifest,
    RepairSearchSpec,
    RepairSpace,
)
from agentgit.core.research import AgentConfig
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import RecordedRange
from agentgit.experiments.validation import frozen_inputs
from agentgit.repair.policy import validate_policy
from agentgit.repair.space import enumerate_proposals, patch_ref, proposal_key
from agentgit.services.debug_reports import DebugReportService
from agentgit.services.environment import source_revision
from agentgit.services.history import HistoryService
from agentgit.services.hybrid import freeze_policy
from agentgit.services.interventions import InterventionService
from agentgit.services.repair_jobs import record_repair_event
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


@dataclass(frozen=True)
class CompiledRepairSearch:
    manifest: RepairSearchManifest
    proposals: tuple[RepairProposal, ...]


class RepairSearchService:
    def __init__(self, history: HistoryService):
        self.history = history
        self.interventions = InterventionService(history)
        self.validators = ValidatorService(history)
        self.versions = VersionService(history)

    def compile(
        self,
        space: RepairSpace,
        budget: RepairBudget,
        intervention_ids: tuple[str, ...],
        hybrid: HybridPolicy | None = None,
    ) -> CompiledRepairSearch:
        binding = space.binding
        repository = UUID(binding.repository_id)
        RepairSearchSpec(
            space=space, budget=budget, intervention_ids=intervention_ids, hybrid=hybrid
        )
        if len(intervention_ids) != len(space.patches):
            raise AgentGitError(ErrorCode.VALIDATION, "每个搜索原子必须绑定一个已发布干预")
        with self.history.engine.connect() as connection:
            source = self.history._run(connection, repository, UUID(binding.failed_run_id))
            head = source["checkpoint_commit_id"]
            if source["status"] not in ("failed", "budget_exhausted") or head is None:
                raise AgentGitError(ErrorCode.VALIDATION, "搜索来源必须是有检查点的失败终态")
        validator = self.validators.manifest(repository, binding.validator_ref)
        _, corpus = frozen_inputs(self.history.objects, validator)
        if validator.fixture_split != "screen":
            raise AgentGitError(ErrorCode.FORBIDDEN, "修复筛选不能使用确认或回归验收器")
        failure = DebugReportService(self.history).compile(
            repository,
            NewDebugReport(failed_run_id=binding.failed_run_id),
        )
        if not failure.diagnosis.assertions or failure.failed.head_commit != head:
            raise AgentGitError(ErrorCode.VALIDATION, "来源缺少与终态一致的硬失败证据")
        baseline = self.validators.evaluate(
            repository, binding.validator_ref, UUID(binding.failed_run_id), head, persist=False
        )
        if baseline.verdict != "failed":
            raise AgentGitError(ErrorCode.VALIDATION, "原运行未在固定验收条件下确定失败")
        view = self.versions.checkout(repository, binding.base_commit)
        self.versions.select_range(
            repository,
            RecordedRange(
                source_run_id=binding.failed_run_id,
                from_commit=binding.base_commit,
                until_commit=head,
            ),
        )
        if view["manifest"]["environment_ref"] != binding.environment_ref:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "搜索环境摘要与共同起点不同")
        frozen = self.history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)
        if not isinstance(frozen, dict) or "config" not in frozen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索起点缺少冻结配置")
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        validate_policy(binding.policy_version, budget, config)
        if not budget.trial.within(config.limits):
            raise AgentGitError(ErrorCode.FORBIDDEN, "修复单试验预算不能扩大来源授权")
        if config.openai is None:
            if budget.max_cost_micro_usd or budget.pricebook is not None:
                raise AgentGitError(ErrorCode.VALIDATION, "Mock 搜索不得宣称供应商费用")
        elif budget.pricebook != config.openai.pricebook:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "修复费用必须冻结来源模型的价格版本")
        for patch, identity in zip(space.patches, intervention_ids, strict=True):
            definition, payload = self.interventions.get(repository, UUID(identity))
            expected = patch.intervention.model_dump(mode="json")
            actual = definition.model_dump(mode="json")
            if payload != patch.intervention.payload or any(
                actual.get(key) != value for key, value in expected.items() if key != "payload"
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "原子效果与已发布 S7 干预不同")
            context = self.interventions.context(
                repository,
                UUID(binding.failed_run_id),
                binding.base_commit,
                definition.target_event_id,
                binding.validator_ref,
            )
            if any(
                context[key] != actual[key]
                for key in (
                    "source_run_id",
                    "base_commit",
                    "target_commit_id",
                    "target_event_id",
                    "precondition",
                )
            ):
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "干预不再满足搜索共同起点条件")
        manifest = RepairSearchManifest(
            space=space,
            budget=budget,
            intervention_ids=intervention_ids,
            source_head_commit=head,
            baseline=baseline,
            failure_assertions=failure.diagnosis.assertions,
            code_revision=source_revision(),
            hybrid=hybrid,
        )
        indices = {patch_ref(patch): index for index, patch in enumerate(space.patches)}
        proposals = []
        for ordered in enumerate_proposals(space):
            positions = tuple(indices[patch_ref(patch)] for patch in ordered)
            selected = tuple(intervention_ids[index] for index in positions)
            applied = self.interventions.apply(
                repository,
                selected,
                source_run_id=binding.failed_run_id,
                base_commit=binding.base_commit,
                validator_ref=binding.validator_ref,
                config=config,
                snapshot=parse_contract(encode(view["snapshot"]), StateSnapshot),
            )
            hybrid_ref = None
            if hybrid is not None:
                hybrid_ref = freeze_policy(
                    self.history,
                    repository,
                    binding.failed_run_id,
                    binding.base_commit,
                    binding.validator_ref,
                    corpus,
                    hybrid,
                    applied.replacements,
                ).id
            proposals.append(
                RepairProposal(
                    key=proposal_key(binding, ordered),
                    ordered_indices=positions,
                    intervention_ids=selected,
                    patch_refs=tuple(patch_ref(patch) for patch in ordered),
                    hybrid_ref=hybrid_ref,
                )
            )
        return CompiledRepairSearch(manifest, tuple(proposals))

    @staticmethod
    def _response(row: Any) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "repository_id": str(row["repository_id"]),
            "search_id": str(row["id"]),
            "manifest_ref": row["manifest_ref"],
            "proposal_count": row["proposal_count"],
        }

    @staticmethod
    def _cached(
        connection: sa.Connection, actor: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        table = TABLES["repair_searches"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.actor == actor, table.c.request_key == key)
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一搜索幂等键不能用于不同请求")
        return RepairSearchService._response(row)

    def submit(
        self,
        space: RepairSpace,
        budget: RepairBudget,
        intervention_ids: tuple[str, ...],
        *,
        actor: str,
        key: str,
        hybrid: HybridPolicy | None = None,
        _request_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not actor or len(actor) > 200 or not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "搜索操作者和幂等键必须是有界非空字符串")
        RepairSearchSpec(
            space=space, budget=budget, intervention_ids=intervention_ids, hybrid=hybrid
        )
        request = (
            _request_identity
            if _request_identity is not None
            else {
                "space": space.model_dump(mode="json"),
                "budget": budget.model_dump(mode="json"),
                "intervention_ids": list(intervention_ids),
                "hybrid": hybrid.model_dump(mode="json") if hybrid else None,
            }
        )
        digest = object_digest(ObjectType.BLOB, encode(request))
        with self.history.engine.connect() as connection:
            requested_at = connection.execute(sa.select(sa.func.clock_timestamp())).scalar_one()
            cached = self._cached(connection, actor, key, digest)
        if cached is not None:
            return cached
        compiled = self.compile(space, budget, intervention_ids, hybrid)
        manifest = compiled.manifest
        repository, identity = UUID(space.binding.repository_id), uuid4()
        with self.history.engine.begin() as connection:
            scope = encode([actor, "POST /repair-searches", key]).decode()
            connection.execute(
                sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(scope, 0)))
            )
            cached = self._cached(connection, actor, key, digest)
            if cached is not None:
                return cached
            source = (
                connection.execute(
                    sa.select(TABLES["runs"])
                    .where(
                        TABLES["runs"].c.repository_id == repository,
                        TABLES["runs"].c.id == UUID(space.binding.failed_run_id),
                    )
                    .with_for_update(read=True)
                )
                .mappings()
                .one()
            )
            if source["checkpoint_commit_id"] != manifest.source_head_commit or source[
                "status"
            ] not in ("failed", "budget_exhausted"):
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "来源终态在发布搜索前发生变化")
            obj = self.history.objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
            self.history._ref(
                connection, repository, obj, "repair_search", str(identity), "manifest"
            )
            values = dict(
                id=identity,
                repository_id=repository,
                failed_run_id=UUID(space.binding.failed_run_id),
                base_commit_id=space.binding.base_commit,
                source_head_commit_id=manifest.source_head_commit,
                manifest_ref=obj.id,
                validator_ref=space.binding.validator_ref,
                actor=actor,
                request_key=key,
                request_digest=digest,
                proposal_count=len(compiled.proposals),
                screen_mode="hybrid" if hybrid else "live",
                created_at=requested_at,
            )
            connection.execute(TABLES["repair_searches"].insert().values(**values))
            connection.execute(
                TABLES["repair_jobs"].insert().values(search_id=identity, repository_id=repository)
            )
            for index, proposal in enumerate(compiled.proposals):
                proposal_id = uuid4()
                body = self.history.objects.put_json(
                    ObjectType.BLOB, proposal.model_dump(mode="json")
                )
                self.history._ref(
                    connection, repository, body, "repair_proposal", str(proposal_id), "definition"
                )
                if proposal.hybrid_ref is not None:
                    hybrid_body = self.history.objects.put(
                        ObjectType.BLOB,
                        self.history.objects.get(proposal.hybrid_ref, ObjectType.BLOB),
                    )
                    self.history._ref(
                        connection,
                        repository,
                        hybrid_body,
                        "repair_proposal",
                        str(proposal_id),
                        "hybrid",
                    )
                connection.execute(
                    TABLES["repair_proposals"]
                    .insert()
                    .values(
                        id=proposal_id,
                        repository_id=repository,
                        search_id=identity,
                        ordinal=index,
                        proposal_key=proposal.key,
                        definition_ref=body.id,
                    )
                )
            record_repair_event(
                self.history,
                connection,
                repository,
                identity,
                0,
                "created",
                {"actor": actor, "proposal_count": len(compiled.proposals)},
            )
            return self._response(values)
