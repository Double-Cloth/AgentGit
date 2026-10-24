"""可重启的有界搜索控制器；Agent 与工具仍由独立研究 Worker 执行。"""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.interventions import PlanPatch, PromptPatch
from agentgit.core.models import parse_contract
from agentgit.core.repair import (
    RepairCandidateComparison,
    RepairConsumption,
    RepairLease,
    RepairProposal,
    RepairSearchDecision,
    RepairSearchManifest,
)
from agentgit.core.research import AgentConfig, AgentState
from agentgit.repair.changes import change_span
from agentgit.services.interventions import InterventionService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_trials import RepairTrialService
from agentgit.services.research import ResearchService
from agentgit.services.versions import VersionService
from agentgit.storage.schema_s9 import TERMINAL_SEARCHES


class RepairSearchWorker:
    def __init__(
        self,
        research: ResearchService,
        *,
        name: str = "repair-worker",
        lease_ms: int = 120_000,
        fault: Callable[[str], None] | None = None,
    ):
        self.history, self.name = research.history, name
        self.ledger = RepairJobService(self.history, lease_ms=lease_ms)
        self.trials = RepairTrialService(research, fault=fault)

    def _view(self, lease: RepairLease) -> dict[str, Any]:
        return self.ledger.view(UUID(lease.repository_id), UUID(lease.search_id))

    def run_once(self, *, repository: UUID | None = None) -> dict[str, Any] | None:
        lease = self.ledger.claim(self.name, repository=repository)
        if lease is None:
            return None
        try:
            return self._advance(lease)
        except AgentGitError as exc:
            if exc.code not in (ErrorCode.VERSION_CONFLICT, ErrorCode.CONTROL_REQUESTED):
                raise
            # 租约被替换或并发取消后，只读取新状态，不能继续派发。
            return self._view(lease)
        finally:
            try:
                self.ledger.release(lease)
            except AgentGitError as exc:
                if exc.code != ErrorCode.VERSION_CONFLICT:
                    raise

    def _advance(self, lease: RepairLease) -> dict[str, Any]:
        view = self._view(lease)
        if view["cancel_requested"]:
            self.trials.request_cancellation(lease)
            self.ledger.abandon_undispatched(lease)
            view = self._view(lease)
            if view["status"] in TERMINAL_SEARCHES:
                return view
        # 最多四项在途；从原意图与归属继续，绝不把未知效果当作未执行。
        for trial in view["trials"]:
            if "dispatch" in trial and "result" not in trial:
                self.trials.settle(lease, UUID(trial["trial_id"]))
                view = self._view(lease)
                if view["status"] in TERMINAL_SEARCHES:
                    return view
        if view["cancel_requested"] or view["status"] == "waiting_reconciliation":
            return view
        for trial in view["trials"]:
            if "dispatch" not in trial and "result" not in trial:
                self.trials.dispatch(lease, UUID(trial["trial_id"]))
        view = self._view(lease)
        recorded = next(
            (t for t in view["trials"] if t["intent"]["operation"] == "recorded_preflight"),
            None,
        )
        if recorded is None:
            return self._reserve(lease, view, None)
        if "result" not in recorded:
            return view
        if recorded["result"]["verdict"] != "chain_valid":
            budget = recorded["result"]["category"] == "budget"
            return self._finish(
                lease,
                view,
                "budget_exhausted" if budget else "needs_attention",
                "recovery_budget_exhausted" if budget else "recovery_chain_failed",
            )
        for _ in range(4):
            if any(t.get("result", {}).get("verdict") == "passed" for t in view["trials"]):
                if view["usage"]["active"]:
                    return view
                return self._finish(lease, view, "needs_confirmation", "screening_passed")
            started = {t["intent"]["proposal_ref"] for t in view["trials"]}
            proposal = next(
                (p for p in view["proposals"] if p["proposal_key"] not in started),
                None,
            )
            if proposal is None:
                if view["usage"]["active"]:
                    return view
                screens = [t for t in view["trials"] if t["intent"]["operation"] == "screen"]
                complete = bool(screens) and all(self._determinate_failure(t) for t in screens)
                return self._finish(
                    lease,
                    view,
                    "no_solution_in_space" if complete else "not_found",
                    "declared_space_exhausted"
                    if complete
                    else ("incomplete_observations" if screens else "no_candidates"),
                )
            before = len(view["trials"])
            view = self._reserve(lease, view, UUID(proposal["proposal_id"]))
            if view["status"] in TERMINAL_SEARCHES or len(view["trials"]) == before:
                return view
        return view

    def _reserve(
        self,
        lease: RepairLease,
        view: dict[str, Any],
        proposal: UUID | None,
    ) -> dict[str, Any]:
        reserved = self.ledger.reserve(lease, proposal, finalize_denial=False)
        if reserved["admitted"]:
            self.trials.dispatch(lease, UUID(reserved["trial_id"]))
            return self._view(lease)
        view = self._view(lease)
        reason = reserved["decision"]["reason"]
        if reason == "concurrency" or view["usage"]["active"]:
            return view
        return self._finish(lease, view, "budget_exhausted", reason)

    @staticmethod
    def _determinate_failure(trial: dict[str, Any]) -> bool:
        result = trial.get("result", {})
        return bool(
            result.get("verdict") == "failed"
            and result.get("determinate")
            and result.get("category") == "oracle"
        )

    def _changes(self, manifest: RepairSearchManifest) -> tuple[int, ...]:
        binding = manifest.space.binding
        repo = UUID(binding.repository_id)
        view = VersionService(self.history).checkout(repo, binding.base_commit)
        state = parse_contract(encode(view["snapshot"]["data"]), AgentState)
        frozen = self.history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)
        if not isinstance(frozen, dict) or "config" not in frozen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索起点缺少冻结配置")
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        service = InterventionService(self.history)
        changes = []
        for patch, identity in zip(manifest.space.patches, manifest.intervention_ids, strict=True):
            payload = patch.intervention.payload
            if isinstance(payload, PromptPatch):
                before, after = config.prompt.encode("utf-8"), payload.prompt.encode("utf-8")
            elif isinstance(payload, PlanPatch):
                before = encode(state.plan.model_dump(mode="json") if state.plan else None)
                after = encode(payload.plan.model_dump(mode="json"))
            else:
                definition, _ = service.get(repo, UUID(identity))
                if definition.replacement_response_ref is None:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "观测补丁缺少替换内容")
                before = service.recording(repo, UUID(definition.source_call_id)).response
                after = self.history.objects.get(
                    definition.replacement_response_ref, ObjectType.BLOB
                )
            changes.append(change_span(before, after))
        return tuple(changes)

    def _finish(
        self,
        lease: RepairLease,
        view: dict[str, Any],
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        manifest = parse_contract(encode(view["manifest"]), RepairSearchManifest)
        screens = [
            t for t in view["trials"] if t["intent"]["operation"] == "screen" and "result" in t
        ]
        passed = {
            t["intent"]["proposal_ref"]: t for t in screens if t["result"]["verdict"] == "passed"
        }
        selected: dict[str, Any] = {}
        comparison: list[RepairCandidateComparison] = []
        if screens:
            changes = self._changes(manifest)
            observed = {trial["intent"]["proposal_ref"]: trial for trial in screens}
            for proposal in view["proposals"]:
                if proposal["proposal_key"] not in observed:
                    continue
                definition = parse_contract(
                    self.history.objects.get(proposal["definition_ref"], ObjectType.BLOB),
                    RepairProposal,
                )
                trial = observed[proposal["proposal_key"]]
                result = trial["result"]
                magnitude = sum(changes[index] for index in definition.ordered_indices)
                report = self.history.objects.get_json(result["replay_report_ref"], ObjectType.BLOB)
                if not isinstance(report, dict) or not isinstance(report.get("actual"), dict):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "排序缺少实际执行报告")
                wall = report["actual"].get("wall_ms")
                if wall is not None and (type(wall) is not int or wall < 0):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实际墙钟计量损坏")
                comparison.append(
                    parse_contract(
                        encode(
                            dict(
                                schema_version=1,
                                proposal_id=proposal["proposal_id"],
                                proposal_key=proposal["proposal_key"],
                                trial_id=trial["trial_id"],
                                result_ref=result["result_ref"],
                                ordinal=proposal["ordinal"],
                                verdict=result["verdict"],
                                category=result["category"],
                                determinate=result["determinate"],
                                safety_violations=result["safety_violations"],
                                patch_count=len(definition.ordered_indices),
                                change_bytes=magnitude,
                                usage=result["usage"],
                                wall_ms=wall,
                            )
                        ),
                        RepairCandidateComparison,
                    )
                )
        comparison.sort(key=lambda row: row.rank_key())
        best = next((row for row in comparison if row.verdict == "passed"), None)
        if best is not None:
            selected = dict(
                selected_proposal_id=best.proposal_id,
                selected_proposal_key=best.proposal_key,
                selected_result_ref=best.result_ref,
                selected_change_bytes=best.change_bytes,
            )
        # 变更幅度读取在事务外；封存时复核 revision，防止覆盖并发取消或新结算。
        with self.history.engine.begin() as connection:
            job = self.ledger._leased(connection, lease)
            if job["revision"] != view["revision"] or job["cancel_requested"]:
                return view
            root = self.ledger._root(connection, UUID(lease.repository_id), UUID(lease.search_id))
            usage = self.ledger._consumption(connection, root)
            if usage.active:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "在途试验结算前不能封存搜索")
            decision = RepairSearchDecision.model_validate(
                dict(
                    search_id=lease.search_id,
                    manifest_ref=view["manifest_ref"],
                    policy_version=manifest.space.binding.policy_version,
                    status=status,
                    reason=reason,
                    total_proposals=len(view["proposals"]),
                    explored_proposals=len(screens),
                    unexplored_proposals=len(view["proposals"]) - len(screens),
                    passed_proposals=len(passed),
                    determinate_failures=sum(self._determinate_failure(t) for t in screens),
                    excluded_atoms=len(manifest.space.excluded_patch_refs),
                    result_refs=tuple(
                        t["result"]["result_ref"] for t in view["trials"] if "result" in t
                    ),
                    comparison=tuple(comparison),
                    usage=RepairConsumption.model_validate(usage),
                    uncertainties=(
                        "仅涵盖已选入的声明空间；排除项和未探索项没有结论。",
                        "尚未执行独立确认、回归和逐项删除；不构成 verified 或最小性证明。",
                    ),
                    **selected,
                )
            )
            self.ledger._update(
                connection,
                job,
                "search_stopped",
                decision.model_dump(mode="json"),
                status=status,
                stop_reason=reason,
                lease_token=None,
                lease_worker=None,
                lease_until=None,
            )
        return self._view(lease)
