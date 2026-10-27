"""S10 离线评估的方法选择；仅接收冻结候选，不接触任务标签或验收结果。"""

from dataclasses import dataclass
from typing import Literal

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import RepairBudget, RepairSearchSpec, RepairSpace
from agentgit.core.research import evolve
from agentgit.repair.policy import MOCK_POLICY, mock_policy_version
from agentgit.repair.space import patch_ref

Method = Literal[
    "repair",
    "first_exception_local",
    "global_prompt_once",
    "unordered_enumeration",
    "combination_without_reduction",
]
METHODS: tuple[Method, ...] = (
    "repair",
    "first_exception_local",
    "global_prompt_once",
    "unordered_enumeration",
    "combination_without_reduction",
)
GLOBAL_PROMPT_TEMPLATE = "prompt-verified-sources/v1"
PROTOCOL = "repair-benchmark-methods/v1"


@dataclass(frozen=True)
class MethodPlan:
    method: Method
    space: RepairSpace
    budget: RepairBudget
    intervention_ids: tuple[str, ...]
    max_reduce_trials: int
    field_policy: Literal["predefined-fields/v1"] | None

    def identity(self) -> str:
        return object_digest(
            ObjectType.BLOB,
            encode(
                dict(
                    schema_version=1,
                    kind=PROTOCOL,
                    method=self.method,
                    space=self.space.model_dump(mode="json"),
                    budget=self.budget.model_dump(mode="json"),
                    intervention_ids=list(self.intervention_ids),
                    max_reduce_trials=self.max_reduce_trials,
                    field_policy=self.field_policy,
                )
            ),
        )


def common_budget_ref(budget: RepairBudget) -> str:
    # 只允许不缩减基线在 screen/reduce 间预先重新分配次数；所有总上限仍相同。
    value = budget.model_dump(mode="json")
    quotas = value.pop("quotas")
    value["screen_reduce_trials"] = quotas["screen"] + quotas["reduce"]
    value["confirm_trials"] = quotas["confirm"]
    value["regression_trials"] = quotas["regression"]
    return object_digest(ObjectType.BLOB, encode(value))


def method_plan(
    method: Method,
    space: RepairSpace,
    budget: RepairBudget,
    intervention_ids: tuple[str, ...],
    *,
    first_exception_target: int | None,
) -> MethodPlan:
    if method not in METHODS:
        raise AgentGitError(ErrorCode.VALIDATION, "未知评估方法")
    RepairSearchSpec(space=space, budget=budget, intervention_ids=intervention_ids)
    if not space.binding.policy_version.startswith(MOCK_POLICY + "@"):
        raise AgentGitError(ErrorCode.FORBIDDEN, "首版评估只允许明确冻结的离线 Mock")
    if space.candidate_source != "full_candidates_fallback":
        raise AgentGitError(
            ErrorCode.FORBIDDEN, "首版仅验证完整候选降级，不接受未经达标核验的排序标记"
        )
    if space.binding.policy_version != mock_policy_version(budget):
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "输入空间与共同预算策略不同")
    if len(intervention_ids) != len(space.patches) or len(set(intervention_ids)) != len(
        intervention_ids
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "干预 ID 必须与冻结原子逐项对应且不重复")
    if first_exception_target is not None and (
        type(first_exception_target) is not int or first_exception_target < 1
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "首异常映射必须是已核对的正整数事件 ID")
    if (
        budget.max_trials,
        budget.quotas.screen,
        budget.quotas.reduce,
        budget.quotas.confirm,
        budget.quotas.regression,
    ) != (30, 14, 6, 6, 4):
        raise AgentGitError(ErrorCode.VALIDATION, "首版评估冻结共同 30 次及 14/6/6/4 阶段策略")
    pairs = list(zip(space.patches, intervention_ids, strict=True))
    # 保留生产模板生成器冻结的顺序；不按结果重排，也不另造主方法排序优势。
    unranked = pairs
    ordered = pairs
    if method == "first_exception_local":
        ordered = [
            item
            for item in unranked
            if item[0].intervention.target_event_id == first_exception_target
        ]
    elif method == "global_prompt_once":
        ordered = [item for item in unranked if item[0].template == GLOBAL_PROMPT_TEMPLATE][:1]
    selected = {patch_ref(patch) for patch, _ in ordered}
    excluded = tuple(
        dict.fromkeys(
            (
                *space.excluded_patch_refs,
                *(patch_ref(patch) for patch in space.patches if patch_ref(patch) not in selected),
            )
        )
    )
    chosen_budget = budget
    if method == "combination_without_reduction":
        chosen_budget = evolve(budget, quotas=evolve(budget.quotas, screen=20, reduce=0))
    assert common_budget_ref(chosen_budget) == common_budget_ref(budget)
    chosen = evolve(
        space,
        patches=tuple(patch for patch, _ in ordered),
        excluded_patch_refs=excluded,
        binding=evolve(space.binding, policy_version=mock_policy_version(chosen_budget)),
        candidate_source="full_candidates_fallback",
        source_reason=f"{PROTOCOL};method={method};仅按冻结候选与公开轨迹选择，不读取标签或试验结果。",
    )
    return MethodPlan(
        method=method,
        space=chosen,
        budget=chosen_budget,
        intervention_ids=tuple(identity for _, identity in ordered),
        max_reduce_trials=chosen_budget.quotas.reduce,
        field_policy=None if method == "combination_without_reduction" else "predefined-fields/v1",
    )
