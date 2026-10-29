"""图外层状态不能伪造调用计量或完成，原生物理数据独立于逻辑投影。"""

import pytest
from pydantic import ValidationError

from agentgit.core.langgraph_runs import LangGraphRunState
from agentgit.core.models import RunStatus
from agentgit.core.research import BudgetUsage


@pytest.mark.parametrize(
    "change",
    [
        {"usage": BudgetUsage(steps=4)},
        {"usage": BudgetUsage(tool_calls=2)},
        {"usage": BudgetUsage(tool_calls=1)},
        {"usage": BudgetUsage(model_calls=1)},
        {"usage": BudgetUsage(tokens=1)},
        {"usage": BudgetUsage(replans=1)},
        {"usage": BudgetUsage(cost_micro_units=1)},
        {"pending": True},
        {"actual_attempts": 1},
        {"native_next": ("user_python",)},
        {"native_next": ("fetch",)},
        {"review_boundary_reached": True},
        {"native_ref": "b" * 64},
        {"status": RunStatus.COMPLETED},
        {"values": {"report": "invented"}},
    ],
)
def test_invalid_usage_and_invented_completion_are_rejected(change):
    with pytest.raises(ValidationError):
        LangGraphRunState(program_ref="a" * 64, **change)


def test_internal_start_boundary_is_preserved_and_physical_refs_are_excluded():
    first = LangGraphRunState(
        program_ref="a" * 64,
        native_ref="b" * 64,
        native_operation="checkpoint",
        native_next=("__start__",),
    )
    other = LangGraphRunState(
        program_ref="a" * 64,
        native_ref="c" * 64,
        native_operation="writes",
        native_next=("__start__",),
    )
    assert first.logical_projection() == other.logical_projection()
