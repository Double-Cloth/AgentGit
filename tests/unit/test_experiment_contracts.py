"""S7 分组条件与预算的白名单边界。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.research import BudgetLimits


def request(**changes):
    return NewExperiment(
        source_run_id=str(uuid4()),
        from_commit="a" * 64,
        validator_ref="b" * 64,
        groups=(ExperimentGroup(key="A", label="基线"), ExperimentGroup(key="B", label="条件")),
        budget=BudgetLimits(),
        **changes,
    )


def test_default_repeats_schedule_ten_trials():
    assert request().repeats * 2 == 10


@pytest.mark.parametrize(
    "extra",
    [
        {"validator": "always_pass"},
        {"validator_ref": "f" * 64},
        {"fixture_split": "confirm"},
        {"tools": ("host_shell",)},
        {"model": "any-model"},
        {"execution_policy": {}},
        {"budget": {"max_tokens": 999999999}},
        {"prompt": ""},
        {"prompt": "x" * 16001},
    ],
)
def test_groups_cannot_change_validator_split_permissions_model_or_budget(extra):
    with pytest.raises(ValidationError):
        ExperimentGroup.model_validate({"key": "B", "label": "拒绝越界", **extra})


@pytest.mark.parametrize("repeats", [0, 11, True, 1.0])
def test_repeats_are_bounded_strict_integers(repeats):
    with pytest.raises(ValidationError):
        request(repeats=repeats)


@pytest.mark.parametrize("keys", [("A", "A"), ("B", "A"), ("B", "B")])
def test_duplicate_or_unordered_groups_are_rejected(keys):
    data = request().model_dump()
    data["groups"] = tuple(ExperimentGroup(key=key, label=key) for key in keys)
    with pytest.raises(ValidationError):
        NewExperiment.model_validate(data)
