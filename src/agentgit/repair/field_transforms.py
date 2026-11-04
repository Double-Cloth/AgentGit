"""只删除追加子句、新增叶节点或新增依赖；不自由生成新内容。"""

import re
from dataclasses import dataclass

from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.interventions import PlanPatch, PromptPatch
from agentgit.core.repair import RepairPatch
from agentgit.core.repair_fields import RepairFieldPlan, RepairFieldVariant
from agentgit.core.repair_validation import RepairCandidate
from agentgit.core.research import Plan, evolve
from agentgit.repair.space import patch_ref


@dataclass(frozen=True)
class GeneratedFields:
    variants: tuple[RepairFieldVariant, ...]
    excluded_count: int
    reasons: tuple[str, ...]


def field_variants(
    plan: RepairFieldPlan,
    candidate: RepairCandidate,
    patches: dict[str, RepairPatch],
    roots: dict[str, str],
) -> GeneratedFields:
    variants: list[RepairFieldVariant] = []
    reasons: list[str] = []
    excluded, seen = 0, set()

    def exclude(reason: str) -> None:
        nonlocal excluded
        excluded += 1
        if len(reasons) < 256:
            reasons.append(reason)

    def append(
        parent: RepairPatch,
        root: str,
        operation: str,
        location: str,
        payload: PromptPatch | PlanPatch,
    ) -> None:
        before_bytes = len(encode(parent.intervention.payload.model_dump(mode="json")))
        after_bytes = len(encode(payload.model_dump(mode="json")))
        if after_bytes >= before_bytes:
            exclude(f"{operation}:{location}:not_smaller")
            return
        intervention = evolve(parent.intervention, payload=payload)
        changed = evolve(parent, intervention=intervention)
        identity = patch_ref(changed)
        if identity in seen:
            return
        seen.add(identity)
        if len(variants) >= plan.max_variants:
            exclude(f"{operation}:{location}:variant_limit")
            return
        variants.append(
            RepairFieldVariant.model_validate(
                dict(
                    root_patch_ref=root,
                    parent_patch_ref=patch_ref(parent),
                    operation=operation,
                    location=location,
                    patch=changed,
                    before_bytes=before_bytes,
                    after_bytes=after_bytes,
                )
            )
        )

    for atom in candidate.atoms:
        parent = patches.get(atom.patch_ref)
        if (
            parent is None
            or atom.operator != parent.intervention.operator
            or atom.patch_ref not in roots
        ):
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "字段候选缺少已核验的父补丁或根来源")
        root = roots[atom.patch_ref]
        payload = parent.intervention.payload
        if isinstance(payload, PromptPatch):
            prefix = plan.source_config.prompt + "\n"
            if not payload.prompt.startswith(prefix):
                exclude("prompt:not_appended_text")
                continue
            suffix = payload.prompt[len(prefix) :]
            spans = list(re.finditer(r".+?(?:[；。！？!?\n]+|$)", suffix, flags=re.DOTALL))
            for index in reversed(range(len(spans))):
                span = spans[index]
                remaining = suffix[: span.start()] + suffix[span.end() :]
                value = plan.source_config.prompt + ("\n" + remaining if remaining else "")
                append(parent, root, "prompt_clause", str(index), PromptPatch(prompt=value))
        elif isinstance(payload, PlanPatch):
            if plan.source_plan is None:
                exclude("plan:no_source_plan")
                continue
            original = {node.key: node for node in plan.source_plan.nodes}
            current = {node.key: node for node in payload.plan.nodes}
            if any(
                key not in current
                or evolve(current[key], dependencies=node.dependencies) != node
                or not set(node.dependencies).issubset(current[key].dependencies)
                or (key in plan.completed and current[key] != node)
                for key, node in original.items()
            ):
                exclude("plan:source_nodes_not_preserved")
                continue
            referenced = {key for node in payload.plan.nodes for key in node.dependencies}
            for node in reversed(payload.plan.nodes):
                if node.key not in original and node.key not in plan.completed:
                    if node.key in referenced:
                        exclude(f"plan_leaf:{node.key}:not_leaf")
                    else:
                        try:
                            changed = Plan(
                                nodes=tuple(n for n in payload.plan.nodes if n.key != node.key)
                            )
                        except ValidationError:
                            exclude(f"plan_leaf:{node.key}:invalid_plan")
                        else:
                            append(parent, root, "plan_leaf", node.key, PlanPatch(plan=changed))
                if node.key in plan.completed:
                    continue
                original_deps = (
                    set(original[node.key].dependencies) if node.key in original else set()
                )
                for dependency in reversed(node.dependencies):
                    if dependency in original_deps:
                        continue
                    changed_node = evolve(
                        node, dependencies=tuple(d for d in node.dependencies if d != dependency)
                    )
                    changed = evolve(
                        payload.plan,
                        nodes=tuple(
                            changed_node if n.key == node.key else n for n in payload.plan.nodes
                        ),
                    )
                    append(
                        parent,
                        root,
                        "plan_dependency",
                        node.key + ":" + dependency,
                        PlanPatch(plan=changed),
                    )
        else:
            exclude("observation:field_change_not_allowed")
    return GeneratedFields(tuple(variants), excluded, tuple(reasons))
