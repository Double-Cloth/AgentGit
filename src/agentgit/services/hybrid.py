"""Hybrid 冻结来源、Job 私有只读输入和耐久观测归属。"""

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.hybrid import FrozenHybridPolicy, HybridCall, HybridPolicy
from agentgit.core.jobs import JobPayload, Lease
from agentgit.core.models import CallOccurrence, EffectClass, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentState, SavedCallOutcome
from agentgit.core.versions import HybridReplaySpec, RecordedRange
from agentgit.experiments.hybrid import (
    HybridSelector,
    ObservationProvenance,
    PreparedObservation,
    slot,
)
from agentgit.services.history import HistoryService
from agentgit.services.managed_calls import ManagedCallContext
from agentgit.services.recordings import RecordingReader
from agentgit.storage.objects import StoredObject
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


def freeze_policy(
    history: HistoryService,
    repository: UUID,
    source_run_id: str,
    from_commit: str,
    validator_ref: str,
    corpus: Corpus,
    policy: HybridPolicy,
    replacements: dict[str, tuple[str, str]],
) -> StoredObject:
    reader = RecordingReader(history)
    view = reader.versions.checkout(repository, from_commit)
    bundle = reader.load(
        repository,
        RecordedRange(
            source_run_id=source_run_id,
            from_commit=from_commit,
            until_commit=policy.until_commit,
        ),
        expected_environment=parse_contract(encode(view["environment"]), EnvironmentManifest),
    )
    selected = {item.source_call_id: item for item in bundle.recordings}
    if set(policy.calls) - selected.keys() or set(replacements) - set(policy.calls):
        raise AgentGitError(
            ErrorCode.RECORDING_MISSING, "Hybrid 选择或响应补丁不属于起点之后的冻结范围"
        )
    calls = []
    total = 0
    for identity in policy.calls:
        record = selected[identity]
        if (
            record.intent.effect != EffectClass.READ_ONLY
            or record.intent.occurrence.provider
            not in ("tool:corpus_search/v1", "tool:source_read/v1")
            or not record.saved.complete
            or (record.saved.error is not None and record.saved.error["code"] == "outcome_unknown")
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "Hybrid 只能选择完整、已知的受控只读响应")
        response, intervention = replacements.get(identity, (None, None))
        total += len(record.request) + len(record.response)
        if response is not None:
            total += len(history.objects.get(response, ObjectType.BLOB))
        if total > 64 * 1024 * 1024:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Hybrid 冻结输入超过 64 MiB")
        calls.append(
            HybridCall(
                source_call_id=identity,
                intent=record.intent,
                saved=record.saved,
                replacement_response_ref=response,
                intervention_id=intervention,
            )
        )
    frozen = FrozenHybridPolicy(
        source_run_id=source_run_id,
        from_commit=from_commit,
        until_commit=policy.until_commit,
        validator_ref=validator_ref,
        corpus_digest=corpus.digest(),
        miss_policy=policy.miss_policy,
        calls=tuple(calls),
    )
    return history.objects.put_json(ObjectType.BLOB, frozen.model_dump(mode="json"))


def register_inputs(
    history: HistoryService,
    connection: Connection,
    repository: UUID,
    job_id: str,
    policy_ref: str,
    *,
    repair_proposal_id: UUID | None = None,
    repair_reduction_trial_id: UUID | None = None,
) -> None:
    refs = TABLES["object_refs"]
    ownership = sa.and_(refs.c.owner_type == "experiment", refs.c.role.like("hybrid:%"))
    if repair_proposal_id is not None and repair_reduction_trial_id is not None:
        raise AgentGitError(ErrorCode.VALIDATION, "Hybrid 输入不能同时属于不同试验")
    if repair_reduction_trial_id is not None:
        trials = TABLES["repair_reduction_trials"]
        if (
            connection.execute(
                sa.select(trials.c.id).where(
                    trials.c.repository_id == repository,
                    trials.c.id == repair_reduction_trial_id,
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.NOT_FOUND, "Hybrid 缩减意图不可访问")
        ownership = sa.and_(
            refs.c.owner_type == "repair_reduction_trial",
            refs.c.owner_id == str(repair_reduction_trial_id),
            refs.c.role == "hybrid",
        )
    if repair_proposal_id is not None:
        proposals = TABLES["repair_proposals"]
        if (
            connection.execute(
                sa.select(proposals.c.id).where(
                    proposals.c.repository_id == repository,
                    proposals.c.id == repair_proposal_id,
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.NOT_FOUND, "Hybrid 修复提案不可访问")
        ownership = sa.and_(
            refs.c.owner_type == "repair_proposal",
            refs.c.owner_id == str(repair_proposal_id),
            refs.c.role == "hybrid",
        )
    if (
        connection.execute(
            sa.select(refs.c.object_id).where(
                refs.c.repository_id == repository,
                refs.c.object_id == policy_ref,
                ownership,
            )
        ).first()
        is None
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "Hybrid policy 没有当前项目实验归属")
    payload = history.objects.get(policy_ref, ObjectType.BLOB)
    frozen = parse_contract(payload, FrozenHybridPolicy)
    documents = [("policy", history.objects.put(ObjectType.BLOB, payload))]
    for index, call in enumerate(frozen.calls):
        for role, identity in (
            ("request", call.intent.request_ref),
            ("response", call.saved.response_ref),
            ("replacement", call.replacement_response_ref),
        ):
            if identity is not None:
                documents.append(
                    (
                        f"{index}:{role}",
                        history.objects.put(
                            ObjectType.BLOB, history.objects.get(identity, ObjectType.BLOB)
                        ),
                    )
                )
    for role, item in documents:
        history._ref(connection, repository, item, "hybrid_input", job_id, role)


def policy_for_run(
    history: HistoryService, repository: UUID, run_id: UUID
) -> tuple[str, FrozenHybridPolicy]:
    jobs = TABLES["jobs"]
    current: UUID | None = run_id
    selected: tuple[UUID, str] | None = None
    with history.engine.connect() as connection:
        for _ in range(100):
            if current is None:
                break
            job = (
                connection.execute(
                    sa.select(jobs).where(
                        jobs.c.repository_id == repository,
                        sa.or_(jobs.c.run_id == current, jobs.c.origin_run_id == current),
                    )
                )
                .mappings()
                .first()
            )
            if job is not None:
                payload = parse_contract(
                    history.objects.get(job["payload_ref"], ObjectType.BLOB), JobPayload
                )
                if not isinstance(payload.replay, HybridReplaySpec):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Hybrid 来源 Job 类型不符")
                selected = (job["id"], payload.replay.policy_ref)
                break
            current = history._run(connection, repository, current)["origin_run_id"]
    if selected is None:
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "Hybrid 来源缺少自身冻结 Job")
    job_id, reference = selected
    return reference, owned_policy(history, repository, job_id, reference, verify_inputs=True)


def owned_policy(
    history: HistoryService,
    repository: UUID,
    job_id: UUID,
    reference: str,
    *,
    verify_inputs: bool = False,
) -> FrozenHybridPolicy:
    refs = TABLES["object_refs"]
    with history.engine.connect() as connection:
        if (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository,
                    refs.c.object_id == reference,
                    refs.c.owner_type == "hybrid_input",
                    refs.c.owner_id == str(job_id),
                    refs.c.role == "policy",
                )
            ).first()
            is None
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "Hybrid 冻结输入不属于该 Job")
    policy = parse_contract(history.objects.get(reference, ObjectType.BLOB), FrozenHybridPolicy)
    if verify_inputs:
        identities = {
            identity
            for call in policy.calls
            for identity in (
                call.intent.request_ref,
                call.saved.response_ref,
                call.replacement_response_ref,
            )
            if identity is not None
        }
        with history.engine.connect() as connection:
            found = set(
                connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == repository,
                        refs.c.object_id.in_(identities),
                        refs.c.owner_type == "hybrid_input",
                        refs.c.owner_id == str(job_id),
                    )
                ).scalars()
            )
        if found != identities:
            raise AgentGitError(ErrorCode.FORBIDDEN, "Hybrid 来源对象缺少冻结 Job 引用")
        for identity in identities:
            history.objects.get(identity, ObjectType.BLOB)
    return policy


class HybridService:
    def __init__(self, history: HistoryService, lease: Lease, reference: str, corpus: Corpus):
        self.history, self.lease, self.reference = history, lease, reference
        self.policy = owned_policy(
            history, lease.repository_id, lease.job_id, reference, verify_inputs=True
        )
        self.selector = HybridSelector(self.policy, corpus, self.read)

    def read(self, reference: str, kind: ObjectType) -> bytes:
        refs = TABLES["object_refs"]
        with self.history.engine.connect() as connection:
            if (
                connection.execute(
                    sa.select(refs.c.object_id).where(
                        refs.c.repository_id == self.lease.repository_id,
                        refs.c.object_id == reference,
                        refs.c.owner_type == "hybrid_input",
                        refs.c.owner_id == str(self.lease.job_id),
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.FORBIDDEN, "Hybrid 读取超出冻结 Job 输入")
        return self.history.objects.get(reference, kind)

    def resolve(
        self, occurrence: CallOccurrence, effect: EffectClass, request: bytes, max_bytes: int
    ) -> PreparedObservation | None:
        try:
            return self.selector.resolve(occurrence, effect, request, max_bytes)
        except AgentGitError as error:
            selected = self.selector.calls.get(slot(occurrence))
            if selected is None or error.code in (
                ErrorCode.OBJECT_CORRUPT,
                ErrorCode.FORBIDDEN,
                ErrorCode.RECORDING_MISSING,
                ErrorCode.OUTCOME_UNKNOWN,
                ErrorCode.VERSION_CONFLICT,
            ):
                raise
            return PreparedObservation(
                b"",
                error,
                ObservationProvenance(
                    kind="miss",
                    selected_call_id=selected.source_call_id,
                    matched=False,
                    response_ref=object_digest(ObjectType.BLOB, b""),
                ),
            )

    def require_reached(self, state: AgentState) -> None:
        self.selector.require_reached(state.messages)

    def record(
        self,
        connection: Connection,
        context: ManagedCallContext,
        observation: PreparedObservation,
        provenance: StoredObject,
    ) -> None:
        self.history._ref(
            connection,
            self.lease.repository_id,
            provenance,
            "hybrid_observation",
            str(context.call_id),
            "provenance",
        )
        connection.execute(
            TABLES["hybrid_observations"]
            .insert()
            .values(
                repository_id=self.lease.repository_id,
                job_id=self.lease.job_id,
                run_id=context.run_id,
                call_id=context.call_id,
                source_call_id=UUID(observation.provenance.selected_call_id),
                policy_ref=self.reference,
                provenance_ref=provenance.id,
                kind=observation.provenance.kind,
            )
        )


def observations(history: HistoryService, repository: UUID, job_id: UUID) -> list[dict[str, Any]]:
    table, calls = TABLES["hybrid_observations"], TABLES["external_calls"]
    with history.engine.connect() as connection:
        rows = list(
            connection.execute(
                sa.select(
                    table,
                    calls.c.logical_step_id,
                    calls.c.call_index,
                    calls.c.attempt,
                    calls.c.provider,
                    calls.c.result_ref,
                )
                .select_from(table.join(calls, calls.c.id == table.c.call_id))
                .where(
                    table.c.repository_id == repository,
                    table.c.job_id == job_id,
                )
                .order_by(calls.c.logical_step_id, calls.c.attempt)
                .limit(101)
            ).mappings()
        )
    if len(rows) > 100:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "Hybrid 观测超过冻结选择上限")
    result = []
    policies: dict[str, FrozenHybridPolicy] = {}
    for row in rows:
        proof = parse_contract(
            history.objects.get(row["provenance_ref"], ObjectType.BLOB), ObservationProvenance
        )
        if proof.kind != row["kind"] or proof.selected_call_id != str(row["source_call_id"]):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Hybrid 观测索引与来源不符")
        if row["policy_ref"] not in policies:
            policies[row["policy_ref"]] = owned_policy(
                history, repository, job_id, row["policy_ref"]
            )
        policy = policies[row["policy_ref"]]
        selected = next(
            (call for call in policy.calls if call.source_call_id == proof.selected_call_id), None
        )
        saved = parse_contract(
            history.objects.get(row["result_ref"], ObjectType.CALL), SavedCallOutcome
        )
        if (
            selected is None
            or slot(selected.intent.occurrence)
            != (row["logical_step_id"], row["call_index"], row["attempt"])
            or saved.response_ref != proof.response_ref
            or not saved.complete
            or (proof.kind in ("recorded", "synthetic")) != proof.matched
            or (proof.kind == "fixture" and policy.miss_policy != "fixture")
            or (proof.kind == "miss" and saved.error is None)
            or (proof.kind != "miss" and row["provider"] != selected.intent.occurrence.provider)
            or (proof.kind != "synthetic" and proof.intervention_id is not None)
            or (proof.kind == "recorded" and proof.response_ref != selected.saved.response_ref)
            or (
                proof.kind == "synthetic"
                and (
                    proof.response_ref != selected.replacement_response_ref
                    or proof.intervention_id != selected.intervention_id
                )
            )
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Hybrid 观测内容与冻结选择或保存结果不符")
        result.append(
            {
                "call_id": str(row["call_id"]),
                "run_id": str(row["run_id"]),
                "logical_step_id": row["logical_step_id"],
                "attempt": row["attempt"],
                "provider": row["provider"],
                **proof.model_dump(mode="json"),
            }
        )
    return result
