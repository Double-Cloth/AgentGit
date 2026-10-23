"""将来源事件和安全检查点核验为不可变干预，拒绝任意规则或权限补丁。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentManifest
from agentgit.core.interventions import (
    Intervention,
    InterventionPrecondition,
    NewIntervention,
    PlanPatch,
    PromptPatch,
    ToolResponsePatch,
)
from agentgit.core.models import (
    CallIntent,
    CallOccurrence,
    EffectClass,
    EnvironmentManifest,
    parse_contract,
)
from agentgit.core.research import AgentConfig, AgentState, Plan, evolve
from agentgit.core.snapshots import EventPayload, StateSnapshot
from agentgit.core.versions import LiveOverrides, RecordedRange
from agentgit.experiments.hybrid import validate_observation
from agentgit.experiments.validation import frozen_inputs
from agentgit.replay.live import prepare_live
from agentgit.replay.recorded import Recording
from agentgit.services.history import HistoryService
from agentgit.services.recordings import RecordingReader
from agentgit.services.validators import ValidatorService
from agentgit.services.versions import VersionService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


@dataclass(frozen=True)
class AppliedInterventions:
    config: AgentConfig | None
    plan: Plan | None
    replacements: dict[str, tuple[str, str]]


class InterventionService:
    def __init__(self, history: HistoryService):
        self.history, self.versions = history, VersionService(history)
        self.reader, self.validators = RecordingReader(history), ValidatorService(history)

    def context(
        self,
        repository: UUID,
        source_run_id: UUID,
        base_commit: str,
        target_event_id: int,
        validator_ref: str,
    ) -> dict[str, Any]:
        events, calls = TABLES["events"], TABLES["external_calls"]
        with self.history.engine.connect() as connection:
            event = (
                connection.execute(
                    sa.select(events).where(
                        events.c.repository_id == repository,
                        events.c.run_id == source_run_id,
                        events.c.id == target_event_id,
                    )
                )
                .mappings()
                .first()
            )
            if event is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "干预上下文的目标事件不可访问")
            if event["type"] not in ("llm.requested", "tool.started", "call.reused"):
                raise AgentGitError(ErrorCode.VALIDATION, "目标事件不是调用开始")
            data = parse_contract(
                self.history.objects.get(event["payload_ref"], ObjectType.EVENT), EventPayload
            ).data
            occurrence = parse_contract(encode(data.get("occurrence")), CallOccurrence)
            call_id = connection.execute(
                sa.select(calls.c.id).where(
                    calls.c.repository_id == repository,
                    calls.c.run_id == source_run_id,
                    calls.c.logical_step_id == occurrence.logical_step_id,
                    calls.c.call_index == occurrence.call_index,
                    calls.c.attempt == occurrence.attempt,
                )
            ).scalar_one_or_none()
        if call_id is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "目标事件缺少完整调用")
        record = self.recording(repository, call_id)
        if record.intent.checkpoint is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "目标调用缺少检查点")
        span = self.versions.select_range(
            repository,
            RecordedRange(
                source_run_id=str(source_run_id),
                from_commit=base_commit,
                until_commit=record.intent.checkpoint,
            ),
        )
        if record.intent.occurrence != occurrence or event["seq"] <= span.from_seq:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "目标调用不在起点之后")
        view = self.versions.checkout(repository, base_commit)
        state = parse_contract(encode(view["snapshot"]["data"]), AgentState)
        frozen = self.history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)
        if not isinstance(frozen, dict) or "config" not in frozen:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "起点缺少冻结配置")
        config = parse_contract(encode(frozen["config"]), AgentConfig)
        validator = self.validators.manifest(repository, validator_ref)
        goal, corpus = frozen_inputs(self.history.objects, validator)
        if (
            goal != state.goal
            or frozen.get("goal") != goal.model_dump(mode="json")
            or frozen.get("corpus_digest") != corpus.digest()
            or view["environment"]["evaluation_time"] != validator.evaluation_time
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "上下文与冻结验收输入不同")
        precondition = InterventionPrecondition(
            state_ref=view["manifest"]["state_ref"],
            config_ref=object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json"))),
            validator_ref=validator_ref,
            occurrence=occurrence,
        )
        return {
            "schema_version": 1,
            "source_run_id": str(source_run_id),
            "base_commit": base_commit,
            "target_commit_id": record.intent.checkpoint,
            "target_event_id": target_event_id,
            "target_event_seq": event["seq"],
            "source_call_id": str(call_id),
            "precondition": precondition.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        }

    def recording(self, repository: UUID, call_id: UUID) -> Recording:
        calls = TABLES["external_calls"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository,
                        calls.c.id == call_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "干预来源调用不可访问")
            run = self.history._run(connection, repository, row["run_id"])
        intent = parse_contract(
            self.reader._owned(
                repository,
                row["intent_ref"],
                ObjectType.CALL,
                "external_call",
                str(call_id),
                "intent",
            ),
            CallIntent,
        )
        if intent.checkpoint is None or run["checkpoint_commit_id"] is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "干预调用尚无完整历史检查点")
        view = self.versions.checkout(repository, intent.checkpoint)
        bundle = self.reader.load(
            repository,
            RecordedRange(
                source_run_id=str(row["run_id"]),
                from_commit=intent.checkpoint,
                until_commit=run["checkpoint_commit_id"],
            ),
            expected_environment=parse_contract(encode(view["environment"]), EnvironmentManifest),
        )
        for record in bundle.recordings:
            if record.source_call_id == str(call_id):
                return record
        raise AgentGitError(ErrorCode.RECORDING_MISSING, "干预调用不在已提交的完整历史中")

    def create(
        self, repository: UUID, request: NewIntervention, *, actor: str, connection: Connection
    ) -> dict[str, Any]:
        request = parse_contract(encode(request.model_dump(mode="json")), NewIntervention)
        events, calls = TABLES["events"], TABLES["external_calls"]
        with self.history.engine.connect() as reading:
            event = (
                reading.execute(
                    sa.select(events).where(
                        events.c.repository_id == repository,
                        events.c.id == request.target_event_id,
                        events.c.run_id == UUID(request.source_run_id),
                    )
                )
                .mappings()
                .first()
            )
            if event is None:
                raise AgentGitError(ErrorCode.NOT_FOUND, "干预目标事件不可访问")
            if event["type"] not in ("llm.requested", "tool.started", "call.reused"):
                raise AgentGitError(ErrorCode.VALIDATION, "干预只能指向完整调用的开始事件")
            data = parse_contract(
                self.history.objects.get(event["payload_ref"], ObjectType.EVENT), EventPayload
            ).data
            occurrence = parse_contract(encode(data.get("occurrence")), CallOccurrence)
            row = (
                reading.execute(
                    sa.select(calls).where(
                        calls.c.repository_id == repository,
                        calls.c.run_id == UUID(request.source_run_id),
                        calls.c.logical_step_id == occurrence.logical_step_id,
                        calls.c.call_index == occurrence.call_index,
                        calls.c.attempt == occurrence.attempt,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "干预事件没有匹配的耐久调用")
        record = self.recording(repository, row["id"])
        span = self.versions.select_range(
            repository,
            RecordedRange(
                source_run_id=request.source_run_id,
                from_commit=request.base_commit,
                until_commit=request.target_commit_id,
            ),
        )
        if (
            record.intent.checkpoint != request.target_commit_id
            or record.intent.occurrence != occurrence
            or occurrence != request.precondition.occurrence
            or event["seq"] <= span.from_seq
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "目标事件、调用或检查点关系不符")
        view = self.versions.checkout(repository, request.base_commit)
        snapshot = parse_contract(encode(view["snapshot"]), StateSnapshot)
        state = parse_contract(encode(snapshot.data), AgentState)
        source_input = self.history.objects.get_json(
            view["environment"]["input_ref"], ObjectType.BLOB
        )
        if not isinstance(source_input, dict) or "config" not in source_input:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "干预起点缺少冻结配置")
        config = parse_contract(encode(source_input["config"]), AgentConfig)
        validator = self.validators.manifest(repository, request.precondition.validator_ref)
        goal, corpus = frozen_inputs(self.history.objects, validator)
        if (
            view["manifest"]["state_ref"] != request.precondition.state_ref
            or object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json")))
            != request.precondition.config_ref
            or goal != state.goal
            or source_input.get("corpus_digest") != corpus.digest()
            or source_input.get("goal") != goal.model_dump(mode="json")
            or view["environment"]["evaluation_time"] != validator.evaluation_time
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "干预原状态、配置或冻结验收输入不符")
        payload = request.payload
        replacement = None
        overrides = LiveOverrides()
        if isinstance(payload, (PromptPatch, PlanPatch)):
            if (
                request.target_commit_id != request.base_commit
                or state.pending_tool is not None
                or not occurrence.provider.startswith("llm:")
            ):
                raise AgentGitError(
                    ErrorCode.FORBIDDEN, "Prompt/计划干预必须在目标模型决策前的检查点应用"
                )
            overrides = (
                LiveOverrides(config=evolve(config, prompt=payload.prompt))
                if isinstance(payload, PromptPatch)
                else LiveOverrides(plan=payload.plan)
            )
        else:
            if record.intent.effect != EffectClass.READ_ONLY:
                raise AgentGitError(ErrorCode.FORBIDDEN, "只能替换受控只读工具观测")
            other = self.recording(repository, UUID(payload.replacement_call_id))
            self._same_split(repository, UUID(payload.replacement_call_id), validator.fixture_split)
            if (
                other.intent.occurrence.provider != occurrence.provider
                or other.intent.occurrence.signature_digest != occurrence.signature_digest
                or other.intent.request_ref != record.intent.request_ref
                or other.intent.effect != EffectClass.READ_ONLY
                or not other.saved.complete
                or other.saved.error is not None
                or len(other.response) > record.intent.max_response_bytes
            ):
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "替换观测必须来自同参同签名的完整只读录制"
                )
            validate_observation(occurrence.provider, record.request, other.response)
            replacement = other.saved.response_ref
        # 统一复用安全检查点、已完成计划保护、DAG、权限和工作区界限。
        prepare_live(snapshot, config, overrides, self.history.objects.get)
        for evidence in request.provenance.evidence:
            if (
                connection.execute(
                    sa.select(events.c.id).where(
                        events.c.repository_id == repository,
                        events.c.run_id == UUID(evidence.run_id),
                        events.c.seq == evidence.seq,
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "干预证据事件不可访问")
        identity = uuid4()
        payload_obj = self.history.objects.put_json(
            ObjectType.BLOB, payload.model_dump(mode="json")
        )
        definition = Intervention(
            **request.model_dump(exclude={"payload"}),
            target_event_seq=event["seq"],
            source_call_id=str(row["id"]),
            payload_ref=payload_obj.id,
            actor=actor,
            replacement_response_ref=replacement,
        )
        definition_obj = self.history.objects.put_json(
            ObjectType.BLOB, definition.model_dump(mode="json")
        )
        precondition = self.history.objects.put_json(
            ObjectType.BLOB, request.precondition.model_dump(mode="json")
        )
        provenance = self.history.objects.put_json(
            ObjectType.BLOB, request.provenance.model_dump(mode="json")
        )
        for role, obj in (
            ("definition", definition_obj),
            ("payload", payload_obj),
            ("precondition", precondition),
            ("provenance", provenance),
        ):
            self.history._ref(connection, repository, obj, "intervention", str(identity), role)
        if replacement is not None:
            replacement_obj = self.history.objects.put(
                ObjectType.BLOB, self.history.objects.get(replacement, ObjectType.BLOB)
            )
            self.history._ref(
                connection,
                repository,
                replacement_obj,
                "intervention",
                str(identity),
                "replacement_response",
            )
        connection.execute(
            TABLES["interventions"]
            .insert()
            .values(
                id=identity,
                repository_id=repository,
                source_run_id=UUID(request.source_run_id),
                source_call_id=row["id"],
                base_commit_id=request.base_commit,
                target_commit_id=request.target_commit_id,
                target_event_id=request.target_event_id,
                target_event_seq=event["seq"],
                definition_ref=definition_obj.id,
                payload_ref=payload_obj.id,
                precondition_ref=precondition.id,
                provenance_ref=provenance.id,
                operator=request.operator,
                effect_policy=request.effect_policy,
            )
        )
        return {
            "schema_version": 1,
            "repository_id": str(repository),
            "intervention_id": str(identity),
            "definition_ref": definition_obj.id,
            "payload_ref": payload_obj.id,
        }

    def get(
        self, repository: UUID, intervention_id: UUID
    ) -> tuple[Intervention, PromptPatch | PlanPatch | ToolResponsePatch]:
        table = TABLES["interventions"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository,
                        table.c.id == intervention_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "干预不可访问")
        definition = parse_contract(
            self.history.objects.get(row["definition_ref"], ObjectType.BLOB), Intervention
        )
        raw = self.history.objects.get(row["payload_ref"], ObjectType.BLOB)
        payload: PromptPatch | PlanPatch | ToolResponsePatch
        if definition.operator == "PROMPT_PATCH":
            payload = parse_contract(raw, PromptPatch)
        elif definition.operator == "PLAN_PATCH":
            payload = parse_contract(raw, PlanPatch)
        else:
            payload = parse_contract(raw, ToolResponsePatch)
        if (
            definition.payload_ref != row["payload_ref"]
            or payload.kind != definition.operator
            or definition.source_run_id != str(row["source_run_id"])
            or definition.source_call_id != str(row["source_call_id"])
            or definition.base_commit != row["base_commit_id"]
            or definition.target_commit_id != row["target_commit_id"]
            or definition.target_event_id != row["target_event_id"]
            or definition.target_event_seq != row["target_event_seq"]
            or definition.operator != row["operator"]
            or definition.effect_policy != row["effect_policy"]
            or object_digest(
                ObjectType.BLOB, encode(definition.precondition.model_dump(mode="json"))
            )
            != row["precondition_ref"]
            or object_digest(ObjectType.BLOB, encode(definition.provenance.model_dump(mode="json")))
            != row["provenance_ref"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "干预索引与不可变内容不符")
        return definition, payload

    def _same_split(self, repository: UUID, call_id: UUID, expected: str) -> None:
        calls, runs = TABLES["external_calls"], TABLES["runs"]
        trials, experiments = TABLES["experiment_trials"], TABLES["experiments"]
        with self.history.engine.connect() as connection:
            ref = connection.execute(
                sa.select(experiments.c.manifest_ref)
                .select_from(
                    calls.join(
                        runs,
                        sa.and_(
                            runs.c.repository_id == calls.c.repository_id,
                            runs.c.id == calls.c.run_id,
                        ),
                    )
                    .join(
                        trials,
                        sa.and_(
                            trials.c.repository_id == runs.c.repository_id,
                            trials.c.branch_name == runs.c.branch_name,
                        ),
                    )
                    .join(
                        experiments,
                        sa.and_(
                            experiments.c.repository_id == trials.c.repository_id,
                            experiments.c.id == trials.c.experiment_id,
                        ),
                    )
                )
                .where(calls.c.repository_id == repository, calls.c.id == call_id)
            ).scalar_one_or_none()
        if ref is not None:
            experiment = parse_contract(
                self.history.objects.get(ref, ObjectType.BLOB), ExperimentManifest
            )
            source = self.validators.manifest(repository, experiment.request.validator_ref)
            if source.fixture_split != expected:
                raise AgentGitError(ErrorCode.FORBIDDEN, "不能跨冻结数据 split 借用工具观测")

    def apply(
        self,
        repository: UUID,
        identities: tuple[str, ...],
        *,
        source_run_id: str,
        base_commit: str,
        validator_ref: str,
        config: AgentConfig,
        snapshot: StateSnapshot,
    ) -> AppliedInterventions:
        changed_config, plan = None, None
        replacements: dict[str, tuple[str, str]] = {}
        state_ref = object_digest(ObjectType.STATE, encode(snapshot.model_dump(mode="json")))
        config_ref = object_digest(ObjectType.BLOB, encode(config.model_dump(mode="json")))
        if len(set(identities)) != len(identities) or len(identities) > 8:
            raise AgentGitError(ErrorCode.VALIDATION, "有序干预集合重复或超过八项")
        for identity in identities:
            definition, payload = self.get(repository, UUID(identity))
            if (
                definition.source_run_id != source_run_id
                or definition.base_commit != base_commit
                or definition.precondition.state_ref != state_ref
                or definition.precondition.config_ref != config_ref
                or definition.precondition.validator_ref != validator_ref
            ):
                raise AgentGitError(
                    ErrorCode.REPLAY_MISMATCH, "干预与本次实验的冻结起点或验收器不同"
                )
            if isinstance(payload, PromptPatch):
                if changed_config is not None:
                    raise AgentGitError(ErrorCode.VALIDATION, "同一 Prompt 字段的干预冲突")
                changed_config = evolve(config, prompt=payload.prompt)
            elif isinstance(payload, PlanPatch):
                if plan is not None:
                    raise AgentGitError(ErrorCode.PLAN_INVALID, "同一计划的干预冲突")
                plan = payload.plan
            else:
                if (
                    definition.source_call_id in replacements
                    or definition.replacement_response_ref is None
                ):
                    raise AgentGitError(
                        ErrorCode.VALIDATION, "同一工具观测的干预冲突或缺少冻结响应"
                    )
                replacements[definition.source_call_id] = (
                    definition.replacement_response_ref,
                    identity,
                )
        return AppliedInterventions(changed_config, plan, replacements)
