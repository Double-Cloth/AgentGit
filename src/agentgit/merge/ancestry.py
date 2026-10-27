"""按可达偏序找出全部最佳共同祖先，不用最短距离替代合并基点。"""

import re
from collections.abc import Callable

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import MergeBases

MAX_COMMITS = 10000


def merge_bases(
    ours: str,
    theirs: str,
    parents: Callable[[str], tuple[str, ...]],
    *,
    selected: str | None = None,
    maximum: int = MAX_COMMITS,
) -> MergeBases:
    def digest(value: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise AgentGitError(ErrorCode.VALIDATION, "Commit 必须是完整小写摘要")

    digest(ours)
    digest(theirs)
    if selected is not None:
        digest(selected)
    if type(maximum) is not int or not 1 <= maximum <= MAX_COMMITS:
        raise AgentGitError(ErrorCode.VALIDATION, "历史读取上限必须为 1–10000")
    graph: dict[str, tuple[str, ...]] = {}
    colors: dict[str, int] = {}
    for head in (ours, theirs):
        pending = [(head, False)]
        while pending:
            node, leaving = pending.pop()
            if leaving:
                colors[node] = 2
                continue
            if colors.get(node) == 2:
                continue
            if colors.get(node) == 1:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Commit 父关系包含循环")
            if len(graph) >= maximum:
                raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "共同祖先解析超过有界历史上限")
            values = parents(node)
            if type(values) is not tuple or len(values) > 2:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Commit 父关系非法或重复")
            for value in values:
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Commit 父标识非法")
            if len(set(values)) != len(values):
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "Commit 父关系非法或重复")
            graph[node] = values
            colors[node] = 1
            pending.append((node, True))
            pending.extend((value, False) for value in reversed(values))

    def ancestors(head: str) -> set[str]:
        found: set[str] = set()
        pending = [head]
        while pending:
            node = pending.pop()
            if node not in found:
                found.add(node)
                pending.extend(graph[node])
        return found

    common = ancestors(ours) & ancestors(theirs)
    # 共同集合对祖先闭合；有共同子节点的祖先不是最佳基点。
    dominated = {parent for node in common for parent in graph[node]}
    best = tuple(sorted(common - dominated))
    if selected is not None and selected not in best:
        raise AgentGitError(ErrorCode.VALIDATION, "显式 base 必须是最佳共同祖先之一")
    chosen = selected if selected is not None else (best[0] if len(best) == 1 else None)
    return MergeBases(
        ours=ours,
        theirs=theirs,
        bases=best,
        selected=chosen,
        status="selected" if chosen else ("ambiguous_merge_base" if best else "unrelated"),
    )
