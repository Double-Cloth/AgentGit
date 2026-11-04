"""最佳共同祖先采用可达关系；覆盖交叉合并、损坏和有界图。"""

import random

import pytest

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.merge.ancestry import merge_bases


def h(number):
    return f"{number:064x}"


def graph(edges):
    return {h(node): tuple(h(parent) for parent in parents) for node, parents in edges.items()}


def test_criss_cross_returns_all_best_bases_until_explicitly_selected():
    nodes = graph({0: (), 1: (0,), 2: (0,), 3: (1, 2), 4: (2, 1)})
    result = merge_bases(h(3), h(4), nodes.__getitem__)
    assert result.status == "ambiguous_merge_base" and result.selected is None
    assert result.bases == (h(1), h(2))
    chosen = merge_bases(h(3), h(4), nodes.__getitem__, selected=h(2))
    assert chosen.selected == h(2) and chosen.status == "selected"
    with pytest.raises(AgentGitError):
        merge_bases(h(3), h(4), nodes.__getitem__, selected=h(0))


def test_shortcuts_do_not_make_an_older_ancestor_a_best_base():
    nodes = graph({0: (), 1: (0,), 2: (1,), 3: (2, 0), 4: (2, 0)})
    result = merge_bases(h(3), h(4), nodes.__getitem__)
    assert result.bases == (h(2),) and result.selected == h(2)


def test_same_head_and_unrelated_histories_are_explicit():
    nodes = graph({0: (), 1: ()})
    assert merge_bases(h(0), h(0), nodes.__getitem__).selected == h(0)
    result = merge_bases(h(0), h(1), nodes.__getitem__)
    assert result.bases == () and result.status == "unrelated"


@pytest.mark.parametrize("edges", [{0: (0,)}, {0: (1,), 1: (0,)}, {0: (1, 1), 1: ()}])
def test_corrupt_cycle_or_duplicate_parent_is_rejected(edges):
    nodes = graph(edges)
    with pytest.raises(AgentGitError) as error:
        merge_bases(h(0), h(0), nodes.__getitem__)
    assert error.value.code == ErrorCode.OBJECT_CORRUPT


def test_history_limit_is_on_distinct_union_nodes_and_does_not_recurse():
    nodes = graph({index: (() if index == 0 else (index - 1,)) for index in range(1200)})
    seen = []

    def read(key):
        seen.append(key)
        return nodes[key]

    assert merge_bases(h(1199), h(1000), read, maximum=1200).selected == h(1000)
    assert len(seen) == len(set(seen)) == 1200
    with pytest.raises(AgentGitError) as error:
        merge_bases(h(1199), h(1000), read, maximum=1199)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT


@pytest.mark.parametrize("head", ["a", "A" * 64, "../unsafe", True])
def test_invalid_head_never_calls_reader(head):
    def forbidden(_):
        pytest.fail("非法标识不能读取历史")

    with pytest.raises(AgentGitError):
        merge_bases(head, h(0), forbidden)


def test_generated_dags_match_a_slow_reachability_oracle():
    rng = random.Random(1729)
    for _ in range(80):
        nodes = {
            h(index): tuple(h(parent) for parent in rng.sample(range(index), min(index, 2)))
            for index in range(25)
        }

        def reachable(head, nodes=nodes):
            found = {head}
            for parent in nodes[head]:
                found.update(reachable(parent))
            return found

        left, right = h(rng.randrange(25)), h(rng.randrange(25))
        common = reachable(left) & reachable(right)
        expected = {
            node
            for node in common
            if not any(node in reachable(other) - {other} for other in common)
        }
        result = merge_bases(left, right, nodes.__getitem__)
        assert result.bases == tuple(sorted(expected))


@pytest.mark.parametrize("parents", [(None,), (["invalid"],), (h(0), h(1), h(2)), [h(0)]])
def test_malformed_parent_input_is_a_domain_error(parents):
    with pytest.raises(AgentGitError) as error:
        merge_bases(h(3), h(3), lambda _: parents)
    assert error.value.code == ErrorCode.OBJECT_CORRUPT
