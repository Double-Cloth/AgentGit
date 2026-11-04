"""读取已封存证据生成精确提案；审阅只创建配置版本或实验定义。"""

from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ValidatorManifest
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.repair import RepairConsumption, RepairSearchManifest
from agentgit.core.repair_fields import RepairFieldSummary
from agentgit.core.repair_reviews import (
    DecideRepairReview,
    RepairAdoptionProposal,
    RepairReviewDecision,
    RepairReviewTicket,
    RepairSavedExperiment,
)
from agentgit.core.repair_validation import (
    FinalRepairLock,
    ReductionSummary,
    RepairVerificationSummary,
    validation_ref,
)
from agentgit.core.research import AgentConfig, ResearchGoal, evolve
from agentgit.repair.space import patch_ref
from agentgit.services.environment import source_revision
from agentgit.services.repair_confirmation import RepairConfirmationService
from agentgit.services.repair_fields import RepairFieldService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.research import ResearchService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class RepairReviewService:
    def __init__(self, research: ResearchService, *, fault: Callable[[str], None] | None = None):
        self.research, self.history = research, research.history
        self.ledger = RepairStudyService(self.history)
        self.fault = fault or (lambda _: None)

    @staticmethod
    def identity(repo: UUID, body: Any, actor: str, key: str) -> str:
        if not actor or len(actor) > 200 or not key or len(key) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "审阅操作者与幂等键必须有界非空")
        return object_digest(ObjectType.BLOB, encode([str(repo), body]))

    def cached(
        self, connection: sa.Connection, name: str, actor: str, key: str, digest: str
    ) -> Any:
        table = TABLES[name]
        row = (
            connection.execute(
                sa.select(table).where(table.c.actor == actor, table.c.request_key == key)
            )
            .mappings()
            .first()
        )
        if row is not None and row["request_digest"] != digest:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "同一审阅幂等键不能用于不同请求")
        return row

    def lock(self, connection: sa.Connection, repo: UUID, route: str, actor: str, key: str) -> None:
        connection.execute(
            sa.select(
                sa.func.pg_advisory_xact_lock(
                    sa.func.hashtextextended(encode([route, actor, key]).decode(), 0)
                )
            )
        )
        table = TABLES["repositories"]
        if (
            connection.execute(
                sa.select(table.c.id).where(table.c.id == repo).with_for_update()
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")

    @staticmethod
    def counter(connection: sa.Connection, repo: UUID) -> int:
        table = TABLES["agent_configs"]
        return int(
            connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(table.c.version), 0)).where(
                    table.c.repository_id == repo
                )
            ).scalar_one()
        )

    def _load(
        self, connection: sa.Connection, repo: UUID, identity: UUID
    ) -> tuple[Any, RepairAdoptionProposal]:
        table = TABLES["repair_adoption_proposals"]
        row = (
            connection.execute(
                sa.select(table).where(table.c.repository_id == repo, table.c.id == identity)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "修复建议不可访问")
        proposal = self.ledger.owned(
            connection,
            repo,
            row["manifest_ref"],
            "repair_adoption_proposal",
            identity,
            "manifest",
            RepairAdoptionProposal,
        )
        if (
            proposal.study_id != str(row["study_id"])
            or validation_ref(proposal.lock) != row["lock_ref"]
            or proposal.verification_ref != row["verification_ref"]
            or proposal.source_config_id != str(row["source_config_id"])
            or proposal.source_config_version != row["source_config_version"]
            or proposal.verification.has_observation_patch != row["has_observation"]
            or proposal.verification.production_eligible != row["production_eligible"]
            or (validation_ref(proposal.after) if proposal.after is not None else None)
            != row["after_config_ref"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "修复提案与不可变索引不同")
        return row, proposal

    def _ticket(self, connection: sa.Connection, repo: UUID, row: Any) -> RepairReviewTicket:
        ticket = self.ledger.owned(
            connection,
            repo,
            row["ticket_ref"],
            "repair_review",
            row["id"],
            "ticket",
            RepairReviewTicket,
        )
        if (
            ticket.actor != row["actor"]
            or ticket.proposal_id != str(row["proposal_id"])
            or ticket.expected_config_version != row["expected_config_version"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "审阅票据与其操作者、提案或版本索引不同")
        return ticket

    def _decision(self, connection: sa.Connection, repo: UUID, row: Any) -> dict[str, Any]:
        value = self.ledger.owned(
            connection,
            repo,
            row["decision_ref"],
            "repair_review_decision",
            row["id"],
            "decision",
            RepairReviewDecision,
        )
        if (
            value.proposal_id != str(row["proposal_id"])
            or value.review_id != str(row["review_id"])
            or value.actor != row["actor"]
            or value.decision != row["decision"]
            or value.new_config_id != (str(row["new_config_id"]) if row["new_config_id"] else None)
            or value.saved_experiment_id
            != (str(row["saved_experiment_id"]) if row["saved_experiment_id"] else None)
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "审阅决定与实际效果索引不同")
        root, proposal = self._load(connection, repo, row["proposal_id"])
        sessions = TABLES["repair_review_sessions"]
        session = (
            connection.execute(
                sa.select(sessions).where(
                    sessions.c.repository_id == repo, sessions.c.id == row["review_id"]
                )
            )
            .mappings()
            .one()
        )
        ticket = self._ticket(connection, repo, session)
        if (
            value.ticket_ref != session["ticket_ref"]
            or value.expected_config_version != ticket.expected_config_version
            or value.actor != ticket.actor
            or value.proposal_ref != ticket.proposal_ref
            or value.proposal_id != ticket.proposal_id
            or value.source_config_id != ticket.source_config_id
            or value.source_config_version != ticket.source_config_version
            or value.lock_ref != ticket.lock_ref
            or value.verification_ref != ticket.verification_ref
        ):
            raise AgentGitError(
                ErrorCode.OBJECT_CORRUPT, "审阅决定没有绑定原操作者的确切票据和版本"
            )
        if (
            value.proposal_ref != root["manifest_ref"]
            or value.lock_ref != root["lock_ref"]
            or value.verification_ref != root["verification_ref"]
            or value.source_config_id != proposal.source_config_id
            or value.source_config_version != proposal.source_config_version
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "审阅决定没有绑定原精确提案")
        if row["new_config_id"] is not None:
            configs = TABLES["agent_configs"]
            config = (
                connection.execute(
                    sa.select(configs).where(
                        configs.c.repository_id == repo, configs.c.id == row["new_config_id"]
                    )
                )
                .mappings()
                .one()
            )
            if (
                config["version"] != value.new_config_version
                or config["config_ref"] != root["after_config_ref"]
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实际采纳配置与批准的版本或内容不同")
        response = dict(
            decision_id=str(row["id"]),
            decision_ref=row["decision_ref"],
            decision=value.model_dump(mode="json"),
        )
        if row["saved_experiment_id"] is not None:
            table = TABLES["repair_saved_experiments"]
            saved = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repo, table.c.id == row["saved_experiment_id"]
                    )
                )
                .mappings()
                .one()
            )
            definition = self.ledger.owned(
                connection,
                repo,
                saved["definition_ref"],
                "repair_saved_experiment",
                saved["id"],
                "definition",
                RepairSavedExperiment,
            )
            if (
                saved["proposal_id"] != row["proposal_id"]
                or saved["actor"] != value.actor
                or definition.proposal_ref != root["manifest_ref"]
                or definition.lock_ref != root["lock_ref"]
                or definition.verification_ref != root["verification_ref"]
                or definition.patches != proposal.patches
                or definition.source_manifest_ref != proposal.source_manifest_ref
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "保存的实验定义与精确审阅提案不同")
            response["saved_experiment"] = dict(
                definition_ref=saved["definition_ref"],
                definition=definition.model_dump(mode="json"),
            )
        return response

    def view(self, repo: UUID, identity: UUID) -> dict[str, Any]:
        with self.history.engine.connect() as connection:
            row, proposal = self._load(connection, repo, identity)
            table = TABLES["proposal_reviews"]
            decided = (
                connection.execute(sa.select(table).where(table.c.proposal_id == identity))
                .mappings()
                .first()
            )
            return dict(
                proposal_id=str(identity),
                proposal_ref=row["manifest_ref"],
                proposal=proposal.model_dump(mode="json"),
                current_config_version=self.counter(connection, repo),
                review=self._decision(connection, repo, decided) if decided else None,
            )

    def _build(self, repo: UUID, study: UUID) -> RepairAdoptionProposal:
        confirmed = RepairConfirmationService(self.research).view(repo, study)
        reduced = self.ledger.view(repo, study)
        if (
            confirmed["verification"] is None
            or confirmed["usage"]["active"]
            or not reduced["final_lock"]
            or reduced["cancel_requested"]
        ):
            raise AgentGitError(
                ErrorCode.CONTROL_REQUESTED, "修复建议需要已排空并封存的实际确认与最终锁"
            )
        source = parse_contract(
            encode(self.ledger.screening.view(repo, UUID(reduced["search_id"]))["manifest"]),
            RepairSearchManifest,
        )
        if source.code_revision != source_revision():
            raise AgentGitError(
                ErrorCode.UNSUPPORTED_SCHEMA, "旧代码证据只读保留，不能创建新审阅建议"
            )
        locked = parse_contract(encode(reduced["final_lock"]["lock"]), FinalRepairLock)
        with self.history.engine.connect() as connection:
            run = self.history._run(connection, repo, UUID(source.space.binding.failed_run_id))
            configs = TABLES["agent_configs"]
            config = (
                connection.execute(
                    sa.select(configs).where(
                        configs.c.repository_id == repo, configs.c.id == run["agent_config_id"]
                    )
                )
                .mappings()
                .one()
            )
            before = self.research.config(repo, config["id"], connection=connection)
            environment = self.history.objects.get_json(
                source.space.binding.environment_ref, ObjectType.ENVIRONMENT
            )
            frozen = self.history.objects.get_json(environment["input_ref"], ObjectType.BLOB)
            controlled = parse_contract(encode(frozen["config"]), AgentConfig)
            if evolve(controlled, limits=before.limits) != before:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "正式来源配置与冻结运行输入不一致")
            originals = {patch_ref(p): p for p in source.space.patches}
            patches = tuple(
                originals[a.patch_ref]
                if a.patch_ref in originals
                else RepairFieldService(self.history).resolve_patch(
                    connection, repo, study, source, a.patch_ref
                )[0]
                for a in locked.candidate.atoms
            )
        after = before
        for patch in patches:
            payload = patch.intervention.payload
            if payload.kind == "PROMPT_PATCH":
                after = evolve(after, prompt=payload.prompt)
            elif payload.kind == "PLAN_PATCH":
                after = evolve(after, plan_template=payload.plan)
        observed = any(p.intervention.operator == "TOOL_RESPONSE_PATCH" for p in patches)
        verification = dict(confirmed["verification"])
        reference = verification.pop("summary_ref")
        mutable_fields: tuple[Literal["prompt", "plan_template"], ...] = ("prompt", "plan_template")
        return RepairAdoptionProposal(
            study_id=str(study),
            search_id=reduced["search_id"],
            source_proposal_id=reduced["manifest"]["selected_proposal_id"],
            source_manifest_ref=validation_ref(source),
            source_config_id=str(config["id"]),
            source_config_version=config["version"],
            source_config_ref=config["config_ref"],
            source_environment_ref=source.space.binding.environment_ref,
            source_environment=parse_contract(encode(environment), EnvironmentManifest),
            source_goal=parse_contract(encode(frozen["goal"]), ResearchGoal),
            validator_ref=source.space.binding.validator_ref,
            validator=parse_contract(
                self.history.objects.get(source.space.binding.validator_ref, ObjectType.BLOB),
                ValidatorManifest,
            ),
            search_budget=source.budget,
            lock=locked,
            verification_ref=reference,
            verification=parse_contract(encode(verification), RepairVerificationSummary),
            before=before,
            after=None if observed else after,
            patches=patches,
            reduction=parse_contract(encode(reduced["reduction"]), ReductionSummary),
            fields=parse_contract(encode(reduced["field_reduction"]["summary"]), RepairFieldSummary)
            if reduced.get("field_reduction")
            else None,
            usage=parse_contract(encode(confirmed["usage"]), RepairConsumption),
            code_revision=source.code_revision,
            changed_fields=tuple(
                k for k in mutable_fields if getattr(before, k) != getattr(after, k)
            ),
            adoption_scope="save_experiment_only" if observed else "create_config_version",
        )

    def create(self, repo: UUID, study: UUID, *, actor: str, key: str) -> dict[str, Any]:
        digest = self.identity(repo, dict(study_id=str(study)), actor, key)
        name = "repair_adoption_proposals"
        with self.history.engine.connect() as connection:
            cached = self.cached(connection, name, actor, key, digest)
        if cached is not None:
            return self.view(repo, cached["id"])
        proposal = self._build(repo, study)
        with self.history.engine.begin() as connection:
            self.lock(connection, repo, name, actor, key)
            cached = self.cached(connection, name, actor, key, digest)
            if cached is not None:
                identity = cached["id"]
            else:
                job = self.ledger._job(connection, repo, study)
                if job["cancel_requested"] or proposal.code_revision != source_revision():
                    raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "提案准备期间控制或代码改变")
                table = TABLES[name]
                existing = connection.execute(
                    sa.select(table.c.id).where(table.c.study_id == study)
                ).scalar_one_or_none()
                if existing is not None:
                    raise AgentGitError(
                        ErrorCode.VERSION_CONFLICT, "同一 study 已有唯一精确修复建议"
                    )
                identity = uuid4()
                obj = self.history.objects.put_json(
                    ObjectType.BLOB, proposal.model_dump(mode="json")
                )
                self.history._ref(
                    connection, repo, obj, "repair_adoption_proposal", str(identity), "manifest"
                )
                after = None
                if proposal.after is not None:
                    after = self.history.objects.put_json(
                        ObjectType.BLOB, proposal.after.model_dump(mode="json")
                    )
                    self.history._ref(
                        connection,
                        repo,
                        after,
                        "repair_adoption_proposal",
                        str(identity),
                        "after_config",
                    )
                connection.execute(
                    table.insert().values(
                        id=identity,
                        repository_id=repo,
                        study_id=study,
                        manifest_ref=obj.id,
                        lock_ref=validation_ref(proposal.lock),
                        verification_ref=proposal.verification_ref,
                        source_config_id=UUID(proposal.source_config_id),
                        source_config_version=proposal.source_config_version,
                        after_config_ref=after.id if after else None,
                        has_observation=proposal.verification.has_observation_patch,
                        production_eligible=proposal.verification.production_eligible,
                        actor=actor,
                        request_key=key,
                        request_digest=digest,
                    )
                )
        return self.view(repo, identity)

    def begin(self, repo: UUID, identity: UUID, *, actor: str, key: str) -> dict[str, Any]:
        name = "repair_review_sessions"
        digest = self.identity(repo, dict(proposal_id=str(identity)), actor, key)
        with self.history.engine.begin() as connection:
            self.lock(connection, repo, name, actor, key)
            cached = self.cached(connection, name, actor, key, digest)
            if cached is not None:
                row = cached
            else:
                root, proposal = self._load(connection, repo, identity)
                reviews = TABLES["proposal_reviews"]
                if connection.execute(
                    sa.select(reviews.c.id).where(reviews.c.proposal_id == identity)
                ).first():
                    raise AgentGitError(ErrorCode.VERSION_CONFLICT, "修复建议已经有最终审阅决定")
                if proposal.code_revision != source_revision():
                    raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "旧代码提案不能开启新审阅")
                ticket = RepairReviewTicket(
                    proposal_id=str(identity),
                    proposal_ref=root["manifest_ref"],
                    expected_config_version=self.counter(connection, repo),
                    source_config_id=proposal.source_config_id,
                    source_config_version=proposal.source_config_version,
                    lock_ref=root["lock_ref"],
                    verification_ref=root["verification_ref"],
                    actor=actor,
                )
                review = uuid4()
                body = self.history.objects.put_json(
                    ObjectType.BLOB, ticket.model_dump(mode="json")
                )
                self.history._ref(connection, repo, body, "repair_review", str(review), "ticket")
                row = dict(
                    id=review,
                    repository_id=repo,
                    proposal_id=identity,
                    ticket_ref=body.id,
                    expected_config_version=ticket.expected_config_version,
                    actor=actor,
                    request_key=key,
                    request_digest=digest,
                )
                connection.execute(TABLES[name].insert().values(**row))
            ticket = self._ticket(connection, repo, row)
            return dict(
                review_id=str(row["id"]),
                ticket_ref=row["ticket_ref"],
                ticket=ticket.model_dump(mode="json"),
            )

    def decide(
        self, repo: UUID, identity: UUID, request: DecideRepairReview, *, actor: str, key: str
    ) -> dict[str, Any]:
        name = "proposal_reviews"
        digest = self.identity(
            repo,
            dict(proposal_id=str(identity), request=request.model_dump(mode="json")),
            actor,
            key,
        )
        with self.history.engine.begin() as connection:
            self.lock(connection, repo, name, actor, key)
            cached = self.cached(connection, name, actor, key, digest)
            if cached is not None:
                return self._decision(connection, repo, cached)
            root, proposal = self._load(connection, repo, identity)
            table = TABLES[name]
            if connection.execute(
                sa.select(table.c.id).where(table.c.proposal_id == identity)
            ).first():
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "建议已有最终决定，不能改决定或重复创建版本"
                )
            sessions = TABLES["repair_review_sessions"]
            row = (
                connection.execute(
                    sa.select(sessions).where(
                        sessions.c.repository_id == repo,
                        sessions.c.id == UUID(request.review_id),
                        sessions.c.proposal_id == identity,
                    )
                )
                .mappings()
                .first()
            )
            if row is None or row["actor"] != actor:
                raise AgentGitError(ErrorCode.NOT_FOUND, "审阅票据不可访问")
            ticket = self._ticket(connection, repo, row)
            if (
                ticket.proposal_ref != root["manifest_ref"]
                or request.proposal_ref != root["manifest_ref"]
                or ticket.expected_config_version != request.expected_config_version
                or self.counter(connection, repo) != ticket.expected_config_version
                or ticket.source_config_id != proposal.source_config_id
                or ticket.source_config_version != proposal.source_config_version
                or ticket.lock_ref != root["lock_ref"]
                or ticket.verification_ref != root["verification_ref"]
            ):
                raise AgentGitError(
                    ErrorCode.VERSION_CONFLICT, "提案、配置版本或审阅绑定已改变，需要重新审阅"
                )
            if proposal.code_revision != source_revision():
                raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "旧代码提案只能只读保留")
            control = self.ledger._job(connection, repo, UUID(proposal.study_id))
            if control["cancel_requested"]:
                raise AgentGitError(ErrorCode.CONTROL_REQUESTED, "已取消的研究不能采纳")
            accepted = proposal.verification.status in (
                "verified_under_fixture",
                "empirically_confirmed",
            )
            if request.decision != "reject" and (
                not accepted or control["status"] != proposal.verification.status
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "确认、回归或安全条件不足，不能批准")
            new_config = None
            saved = None
            if request.decision == "approve":
                if proposal.after is None or not proposal.verification.production_eligible:
                    raise AgentGitError(ErrorCode.FORBIDDEN, "工具观测替换只能保存为实验")
                new_config = self.research.create_config(
                    repo,
                    proposal.after,
                    connection=connection,
                    expected_version=request.expected_config_version,
                )
                self.fault("after_config_created")
            elif request.decision == "save_experiment":
                saved = uuid4()
                definition = self.history.objects.put_json(
                    ObjectType.BLOB,
                    dict(
                        schema_version=1,
                        kind="repair_saved_experiment/v1",
                        proposal_ref=root["manifest_ref"],
                        lock_ref=root["lock_ref"],
                        verification_ref=root["verification_ref"],
                        patches=[p.model_dump(mode="json") for p in proposal.patches],
                        source_manifest_ref=proposal.source_manifest_ref,
                        execution="not_started",
                        test_only=True,
                    ),
                )
                self.history._ref(
                    connection,
                    repo,
                    definition,
                    "repair_saved_experiment",
                    str(saved),
                    "definition",
                )
                connection.execute(
                    TABLES["repair_saved_experiments"]
                    .insert()
                    .values(
                        id=saved,
                        repository_id=repo,
                        proposal_id=identity,
                        definition_ref=definition.id,
                        actor=actor,
                    )
                )
            decision = RepairReviewDecision(
                proposal_id=str(identity),
                review_id=request.review_id,
                proposal_ref=root["manifest_ref"],
                ticket_ref=row["ticket_ref"],
                expected_config_version=request.expected_config_version,
                decision=request.decision,
                actor=actor,
                source_config_id=proposal.source_config_id,
                source_config_version=proposal.source_config_version,
                new_config_id=str(new_config) if new_config else None,
                new_config_version=request.expected_config_version + 1 if new_config else None,
                saved_experiment_id=str(saved) if saved else None,
                lock_ref=root["lock_ref"],
                verification_ref=root["verification_ref"],
            )
            decision_id = uuid4()
            body = self.history.objects.put_json(ObjectType.BLOB, decision.model_dump(mode="json"))
            self.history._ref(
                connection, repo, body, "repair_review_decision", str(decision_id), "decision"
            )
            decision_row = dict(
                id=decision_id,
                repository_id=repo,
                proposal_id=identity,
                review_id=UUID(request.review_id),
                decision=request.decision,
                decision_ref=body.id,
                new_config_id=new_config,
                saved_experiment_id=saved,
                actor=actor,
                request_key=key,
                request_digest=digest,
            )
            connection.execute(table.insert().values(**decision_row))
            self.fault("before_decision_commit")
            response = self._decision(connection, repo, decision_row)
        self.fault("after_decision_commit")
        return response
