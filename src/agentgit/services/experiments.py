"""冻结 A/B 条件并原子排队；执行与恢复复用现有持久 Job/Worker。"""

from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from agentgit.core.canonical import ObjectType, decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import ExperimentManifest, NewExperiment
from agentgit.core.jobs import TERMINAL_RUNS
from agentgit.core.models import EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, AgentState, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.core.versions import LiveOverrides, LiveReplay, RecordedRange
from agentgit.experiments.statistics import experiment_statistics
from agentgit.experiments.validation import frozen_inputs
from agentgit.services.hybrid import freeze_policy
from agentgit.services.interventions import InterventionService
from agentgit.services.jobs import JobService
from agentgit.services.replays import ReplayService
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class ExperimentService:
    def __init__(self, research: ResearchService):
        self.research, self.history = research, research.history
        self.replays = ReplayService(research)
        self.validators = ValidatorService(self.history)
        self.jobs = JobService(research)
        self.interventions = InterventionService(self.history)

    def submit(
        self,
        repository_id: UUID,
        request: NewExperiment,
        *,
        actor: str,
        connection: Connection,
    ) -> dict[str, Any]:
        # 调用方的幂等事务覆盖全部 trial；部分失败不能留下可见的半个实验。
        request = parse_contract(encode(request.model_dump(mode="json")), NewExperiment)
        if not actor or len(actor) > 200:
            raise AgentGitError(ErrorCode.VALIDATION, "实验操作者身份无效")
        validator = self.validators.manifest(repository_id, request.validator_ref)
        goal, corpus = frozen_inputs(self.history.objects, validator)
        self.replays.versions.select_range(
            repository_id,
            RecordedRange(
                source_run_id=request.source_run_id,
                from_commit=request.from_commit,
                until_commit=request.from_commit,
            ),
        )
        view = self.replays.versions.checkout(repository_id, request.from_commit)
        state = parse_contract(encode(view["snapshot"]["data"]), AgentState)
        environment = parse_contract(encode(view["environment"]), EnvironmentManifest)
        source_corpus = self.replays._source_corpus(repository_id, UUID(request.source_run_id))
        if (
            state.goal != goal
            or source_corpus.digest() != corpus.digest()
            or environment.evaluation_time != validator.evaluation_time
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "实验共同起点与冻结验收输入或语料不同")
        source_input = decode(self.history.objects.get(environment.input_ref, ObjectType.BLOB))
        if not isinstance(source_input, dict) or "config" not in source_input:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实验来源缺少冻结配置")
        if source_input.get("corpus_digest") != corpus.digest() or source_input.get(
            "goal"
        ) != state.goal.model_dump(mode="json"):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "实验来源的冻结输入或语料摘要无法复核")
        config = parse_contract(encode(source_input["config"]), AgentConfig)
        if not request.budget.within(config.limits):
            raise AgentGitError(ErrorCode.FORBIDDEN, "trial 预算不能扩大来源配置授权")
        config_obj = self.history.objects.put_json(ObjectType.BLOB, config.model_dump(mode="json"))
        manifest = ExperimentManifest(
            mode="hybrid" if request.hybrid else "live",
            request=request,
            source_environment_ref=view["manifest"]["environment_ref"],
            source_state_ref=view["manifest"]["state_ref"],
            source_config_ref=config_obj.id,
        )
        obj = self.history.objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
        experiment_id = uuid4()
        for role, item in (("manifest", obj), ("source_config", config_obj)):
            self.history._ref(
                connection, repository_id, item, "experiment", str(experiment_id), role
            )
        connection.execute(
            TABLES["experiments"]
            .insert()
            .values(
                id=experiment_id,
                repository_id=repository_id,
                source_run_id=UUID(request.source_run_id),
                from_commit_id=request.from_commit,
                manifest_ref=obj.id,
                validator_ref=request.validator_ref,
                repeats=request.repeats,
                actor=actor,
            )
        )
        trials = []
        for group in request.groups:
            # 唯一开放的配置字段是固定 Prompt；工具、模型、权限和预算不可由分组改写。
            changed_config = (
                evolve(config, prompt=group.prompt) if group.prompt is not None else None
            )
            plan = group.plan
            replacements: dict[str, tuple[str, str]] = {}
            if group.interventions:
                applied = self.interventions.apply(
                    repository_id,
                    group.interventions,
                    source_run_id=request.source_run_id,
                    base_commit=request.from_commit,
                    validator_ref=request.validator_ref,
                    config=config,
                    snapshot=parse_contract(encode(view["snapshot"]), StateSnapshot),
                )
                if applied.replacements and request.hybrid is None:
                    raise AgentGitError(
                        ErrorCode.FORBIDDEN, "工具观测干预需要完整 Hybrid 执行上下文"
                    )
                changed_config, plan = applied.config, applied.plan
                replacements = applied.replacements
            hybrid_obj = None
            if request.hybrid is not None:
                hybrid_obj = freeze_policy(
                    self.history,
                    repository_id,
                    request.source_run_id,
                    request.from_commit,
                    request.validator_ref,
                    corpus,
                    request.hybrid,
                    replacements,
                )
                self.history._ref(
                    connection,
                    repository_id,
                    hybrid_obj,
                    "experiment",
                    str(experiment_id),
                    f"hybrid:{group.key}",
                )
            for index in range(request.repeats):
                branch_name = f"exp-{experiment_id.hex}-{group.key}-{index}"
                submitted = self.replays.submit(
                    repository_id,
                    LiveReplay(
                        source_run_id=request.source_run_id,
                        from_commit=request.from_commit,
                        branch_name=branch_name,
                        budget=request.budget,
                        overrides=LiveOverrides(config=changed_config, plan=plan),
                    ),
                    actor=actor,
                    key=f"experiment:{experiment_id}:{group.key}:{index}",
                    connection=connection,
                    hybrid_ref=hybrid_obj.id if hybrid_obj is not None else None,
                )
                connection.execute(
                    TABLES["experiment_trials"]
                    .insert()
                    .values(
                        experiment_id=experiment_id,
                        repository_id=repository_id,
                        group_key=group.key,
                        trial_index=index,
                        job_id=UUID(submitted["job_id"]),
                        initial_run_id=UUID(submitted["run_id"]),
                        branch_name=branch_name,
                    )
                )
                trials.append(
                    {
                        "group": group.key,
                        "index": index,
                        "job_id": submitted["job_id"],
                        "run_id": submitted["run_id"],
                        "branch_name": branch_name,
                    }
                )
        return {
            "schema_version": 1,
            "experiment_id": str(experiment_id),
            "repository_id": str(repository_id),
            "manifest_ref": obj.id,
            "trial_count": len(trials),
            "trials": trials,
        }

    def manifest(self, repository_id: UUID, experiment_id: UUID) -> ExperimentManifest:
        table = TABLES["experiments"]
        with self.history.engine.connect() as connection:
            row = (
                connection.execute(
                    sa.select(table).where(
                        table.c.repository_id == repository_id,
                        table.c.id == experiment_id,
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "实验不可访问")
        manifest = parse_contract(
            self.history.objects.get(row["manifest_ref"], ObjectType.BLOB), ExperimentManifest
        )
        request = manifest.request
        if (
            request.source_run_id != str(row["source_run_id"])
            or request.from_commit != row["from_commit_id"]
            or request.validator_ref != row["validator_ref"]
            or request.repeats != row["repeats"]
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实验索引与冻结 manifest 不符")
        return manifest

    def _projection(
        self, repository_id: UUID, job_id: UUID
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        fields = ("run_id", "status", "checkpoint_commit_id", "attempt", "fencing_token")
        for _ in range(3):
            job = self.jobs.get(repository_id, job_id)
            replay = self.replays.report(repository_id, job_id)
            current = self.jobs.get(repository_id, job_id)
            if any(job[name] != current[name] for name in fields):
                continue
            if replay is None or replay["mode"] not in ("live", "hybrid"):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实验 trial 不再关联冻结的 Live Job")
            return job, replay
        raise AgentGitError(ErrorCode.VERSION_CONFLICT, "trial 正在推进，请刷新实验状态")

    def report(self, repository_id: UUID, experiment_id: UUID) -> dict[str, Any]:
        manifest = self.manifest(repository_id, experiment_id)
        config = parse_contract(
            self.history.objects.get(manifest.source_config_ref, ObjectType.BLOB), AgentConfig
        )
        trials = TABLES["experiment_trials"]
        with self.history.engine.connect() as connection:
            rows = list(
                connection.execute(
                    sa.select(trials)
                    .where(
                        trials.c.repository_id == repository_id,
                        trials.c.experiment_id == experiment_id,
                    )
                    .order_by(trials.c.group_key, trials.c.trial_index)
                    .limit(21)
                ).mappings()
            )
        expected = [(key, index) for key in ("A", "B") for index in range(manifest.request.repeats)]
        if [(row["group_key"], row["trial_index"]) for row in rows] != expected:
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "实验 trial 数量或分组与冻结条件不同")
        items = []
        for row in rows:
            job, replay = self._projection(repository_id, row["job_id"])
            if (
                replay["source_run_id"] != manifest.request.source_run_id
                or replay["from_commit"] != manifest.request.from_commit
                or replay["mode"] != manifest.mode
            ):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "trial 运行来源与冻结实验不符")
            assessment = None
            if (
                job["status"] in TERMINAL_RUNS
                and job["checkpoint_commit_id"] is not None
                and job["checkpoint_commit_id"] != manifest.request.from_commit
            ):
                assessment = self.validators.evaluate(
                    repository_id,
                    manifest.request.validator_ref,
                    job["run_id"],
                    job["checkpoint_commit_id"],
                )
                if assessment.verdict == "passed" and job["status"] != "completed":
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Job 终态与验收检查点不一致")
            items.append(
                {
                    "group": row["group_key"],
                    "index": row["trial_index"],
                    "job_id": str(row["job_id"]),
                    "initial_run_id": str(row["initial_run_id"]),
                    "run_id": str(job["run_id"]),
                    "attempt": job["attempt"],
                    "branch_name": row["branch_name"],
                    "status": job["status"],
                    "checkpoint_commit_id": job["checkpoint_commit_id"],
                    "verdict": assessment.verdict if assessment is not None else "incomplete",
                    "assessment": assessment.model_dump(mode="json")
                    if assessment is not None
                    else None,
                    "replay": replay,
                }
            )
        return {
            "schema_version": 1,
            "experiment_id": str(experiment_id),
            "repository_id": str(repository_id),
            "manifest": manifest.model_dump(mode="json"),
            "trials": items,
            "statistics": experiment_statistics(items),
            "groups": [
                {
                    "key": key,
                    "total": sum(item["group"] == key for item in items),
                    "passed": sum(
                        item["group"] == key and item["verdict"] == "passed" for item in items
                    ),
                    "incomplete": sum(
                        item["group"] == key and item["verdict"] == "incomplete" for item in items
                    ),
                }
                for key in ("A", "B")
            ],
            "test_only": True,
            "limitations": [
                (
                    "只复用显式选定且精确匹配的只读观测；synthetic/模拟结果均为 test_only。"
                    if manifest.mode == "hybrid"
                    else "当前为 Live 条件比较，录制调用不参与本次实验。"
                ),
                (
                    "固定 Mock 使用相同 fixture 与 random_seed=0；"
                    "不据此推断真实模型方差或严格因果。"
                    if config.model == "mock-research/v1"
                    else "供应商模型不保证确定性；random_seed=0 仅作环境记录，费用为价格表估计。"
                ),
                "运行状态按查询时刻展示；未完成、取消、中断和预算耗尽均不能计为通过。",
            ],
        }
