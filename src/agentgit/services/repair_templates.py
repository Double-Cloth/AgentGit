"""从显式共同起点生成全部适用的有限模板；不执行模型或读取评估标签。"""

from dataclasses import dataclass
from uuid import UUID

from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair import (
    NewRepairSearch,
    RepairBinding,
    RepairBudget,
    RepairPatch,
    RepairSpace,
)
from agentgit.core.research import AgentConfig
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.policy import mock_budget, mock_policy_version
from agentgit.repair.space import freeze_space
from agentgit.repair.templates import TemplateAnchor, model_templates, observation_templates
from agentgit.services.history import HistoryService
from agentgit.services.interventions import InterventionService
from agentgit.services.versions import VersionService


@dataclass(frozen=True)
class TemplateSearchPlan:
    space: RepairSpace
    budget: RepairBudget


def prepare_templates(
    history: HistoryService,
    repository: UUID,
    request: NewRepairSearch,
) -> TemplateSearchPlan:
    view = VersionService(history).checkout(repository, request.base_commit)
    frozen = history.objects.get_json(view["environment"]["input_ref"], ObjectType.BLOB)
    if not isinstance(frozen, dict) or "config" not in frozen:
        raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "搜索起点缺少冻结配置")
    config = parse_contract(encode(frozen["config"]), AgentConfig)
    snapshot = parse_contract(encode(view["snapshot"]), StateSnapshot)
    if config.openai is not None and request.budget is None:
        raise AgentGitError(ErrorCode.VALIDATION, "真实供应商搜索必须显式提供费用和有限预算")
    budget = request.budget or mock_budget(config)
    version = mock_policy_version(budget) if config.openai is None else "bounded-templates/v1"
    binding = RepairBinding(
        repository_id=str(repository),
        failed_run_id=request.failed_run_id,
        base_commit=request.base_commit,
        environment_ref=view["manifest"]["environment_ref"],
        validator_ref=request.validator_ref,
        policy_version=version,
    )
    patches: list[RepairPatch] = []
    skipped: list[str] = []
    service = InterventionService(history)
    for target in request.targets:
        context = service.context(
            repository,
            UUID(request.failed_run_id),
            request.base_commit,
            target.event_id,
            request.validator_ref,
        )
        anchor = parse_contract(
            encode(
                {
                    "schema_version": 1,
                    **{
                        key: context[key]
                        for key in (
                            "source_run_id",
                            "base_commit",
                            "target_commit_id",
                            "target_event_id",
                            "precondition",
                        )
                    },
                }
            ),
            TemplateAnchor,
        )
        if anchor.precondition.occurrence.provider.startswith("llm:"):
            if target.replacement_call_ids:
                raise AgentGitError(ErrorCode.FORBIDDEN, "模型决策不能使用工具观测替换录制")
            batch = model_templates(anchor, config, snapshot, history.objects.get)
        else:
            if request.hybrid is None:
                raise AgentGitError(ErrorCode.FORBIDDEN, "观测模板必须显式使用测试 Hybrid 策略")
            batch = observation_templates(anchor, target.replacement_call_ids)
        patches.extend(batch.patches)
        skipped.extend(f"{target.event_id}:{reason}" for reason in batch.skipped)
    space = freeze_space(
        binding,
        patches,
        max_atoms=request.max_atoms,
        max_depth=request.max_depth,
        source_reason=request.source_reason,
        template_skips=tuple(skipped),
    )
    return TemplateSearchPlan(space, budget)
