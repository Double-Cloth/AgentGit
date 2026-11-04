"""先单点后有序双点；重复提案与独立重复试验使用不同身份。"""

from collections.abc import Iterable, Iterator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.repair import RepairBinding, RepairPatch, RepairSpace, RepairTrialIdentity


def patch_ref(patch: RepairPatch) -> str:
    # 解释、模板名称不是效果；目标、前置条件和 payload 均参与身份。
    return object_digest(
        ObjectType.BLOB,
        encode(
            {
                "schema_version": 1,
                "kind": "repair-patch-effect/v1",
                "intervention": patch.intervention.model_dump(mode="json", exclude={"provenance"}),
            }
        ),
    )


def validate_patches(binding: RepairBinding, patches: tuple[RepairPatch, ...]) -> None:
    if not 1 <= len(patches) <= 2:
        raise AgentGitError(ErrorCode.VALIDATION, "搜索只允许一项或两项有序干预")
    fields: set[str] = set()
    states: set[tuple[str, str]] = set()
    refs: set[str] = set()
    for patch in patches:
        request = patch.intervention
        if (
            request.source_run_id != binding.failed_run_id
            or request.base_commit != binding.base_commit
            or request.precondition.validator_ref != binding.validator_ref
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "修复补丁不属于冻结搜索条件")
        if request.operator != "TOOL_RESPONSE_PATCH" and (
            request.target_commit_id != binding.base_commit
            or not request.precondition.occurrence.provider.startswith("llm:")
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "配置或计划只能从同一模型决策前修改")
        occurrence = request.precondition.occurrence
        field: str = request.operator
        if field == "TOOL_RESPONSE_PATCH":
            if occurrence.provider not in ("tool:corpus_search/v1", "tool:source_read/v1"):
                raise AgentGitError(ErrorCode.FORBIDDEN, "修复仅能替换已授权只读工具观测")
            field += f":{occurrence.logical_step_id}:{occurrence.call_index}:{occurrence.attempt}"
        states.add((request.precondition.state_ref, request.precondition.config_ref))
        ref = patch_ref(patch)
        if field in fields or ref in refs:
            raise AgentGitError(ErrorCode.VALIDATION, "组合包含重复或冲突字段，不能覆盖前项")
        fields.add(field)
        refs.add(ref)
    if len(states) != 1:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "组合必须绑定相同起点状态和配置")


def proposal_key(binding: RepairBinding, patches: tuple[RepairPatch, ...]) -> str:
    validate_patches(binding, patches)
    return object_digest(
        ObjectType.BLOB,
        encode(
            {
                "schema_version": 1,
                "kind": "repair-proposal/v1",
                "binding": binding.model_dump(mode="json"),
                "ordered_patches": [patch_ref(patch) for patch in patches],
            }
        ),
    )


def trial_key(search_id: str, proposal_ref: str, phase: str, repeat: int) -> str:
    identity = RepairTrialIdentity.model_validate(
        {
            "search_id": search_id,
            "proposal_ref": proposal_ref,
            "phase": phase,
            "repeat": repeat,
        }
    )
    return object_digest(
        ObjectType.BLOB,
        encode(
            {
                "kind": "repair-trial/v1",
                **identity.model_dump(mode="json"),
            }
        ),
    )


def freeze_space(
    binding: RepairBinding,
    patches: Iterable[RepairPatch],
    *,
    source_reason: str,
    candidate_source: str = "full_candidates_fallback",
    max_atoms: int = 8,
    max_depth: int = 2,
    template_skips: tuple[str, ...] = (),
) -> RepairSpace:
    if type(max_atoms) is not int or not 1 <= max_atoms <= 8:
        raise AgentGitError(ErrorCode.VALIDATION, "原子补丁上限必须在一至八之间")
    selected: list[RepairPatch] = []
    seen: set[str] = set()
    excluded: list[str] = []
    for index, patch in enumerate(patches):
        if index >= 1032:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "修复空间输入超过有限归档上限")
        validate_patches(binding, (patch,))
        ref = patch_ref(patch)
        if ref in seen:
            continue
        seen.add(ref)
        if len(selected) < max_atoms:
            selected.append(patch)
        else:
            excluded.append(ref)
        if len(excluded) > 1024:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "修复空间超过可归档的原子补丁数量")
    return RepairSpace.model_validate(
        {
            "binding": binding,
            "candidate_source": candidate_source,
            "source_reason": source_reason,
            "patches": tuple(selected),
            "excluded_patch_refs": tuple(excluded),
            "max_depth": max_depth,
            "template_skips": template_skips,
        }
    )


def enumerate_proposals(space: RepairSpace) -> Iterator[tuple[RepairPatch, ...]]:
    # 不接收单点执行结果，因此单点全失败不会阻断联合枚举。
    seen: set[str] = set()
    for patch in space.patches:
        validate_patches(space.binding, (patch,))
        ref = patch_ref(patch)
        if ref in seen:
            raise AgentGitError(ErrorCode.VALIDATION, "枚举前必须冻结去重后的原子补丁空间")
        seen.add(ref)
    for patch in space.patches:
        yield (patch,)
    if space.max_depth == 1:
        return
    for first in space.patches:
        for second in space.patches:
            pair = (first, second)
            try:
                validate_patches(space.binding, pair)
            except AgentGitError as exc:
                if exc.code == ErrorCode.VALIDATION:
                    continue
                raise
            yield pair
