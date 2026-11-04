"""用于已探索候选排序的规范字节变更范围；不声称最小编辑距离。"""


def change_span(before: bytes, after: bytes) -> int:
    prefix = 0
    bound = min(len(before), len(after))
    while prefix < bound and before[prefix] == after[prefix]:
        prefix += 1
    suffix = 0
    while suffix < bound - prefix and before[-suffix - 1] == after[-suffix - 1]:
        suffix += 1
    return len(before) + len(after) - 2 * (prefix + suffix)
