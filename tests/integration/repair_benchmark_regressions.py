"""四项固定独立回归输入，覆盖框架顺序、精确数值和时间边界。"""

from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator
from repair_dataset import InputProfile, public_input

from agentgit.adapters.corpus import utc_time
from agentgit.core.models import Contract, parse_contract
from agentgit.core.research import DEFAULT_FRAMEWORKS, evolve

DATA = Path(__file__).resolve().parents[1] / "fixtures/repair/regressions-v1.json"


class RegressionInput(Contract):
    profile: InputProfile
    frameworks: tuple[str, str, str]
    clock_boundary: Literal[
        "none", "effective_at_clock", "expires_after_clock", "collected_at_clock"
    ]


class RegressionSpecifications(Contract):
    kind: Literal["repair-regression-inputs/v1"] = "repair-regression-inputs/v1"
    entries: Annotated[tuple[RegressionInput, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def independent_tasks(self):
        if (
            len({r.profile.key for r in self.entries}) != 4
            or len({r.frameworks for r in self.entries}) != 4
            or len({r.clock_boundary for r in self.entries}) != 4
            or any(set(r.frameworks) != set(DEFAULT_FRAMEWORKS) for r in self.entries)
        ):
            raise ValueError("四项回归必须具有不同输入、框架顺序和时间条件")
        return self


def regression_inputs():
    specs = parse_contract(DATA.read_bytes(), RegressionSpecifications)
    tasks = []
    for item in specs.entries:
        task = public_input(item.profile)
        clock, changes = utc_time(task.evaluation_time), {}
        if item.clock_boundary == "effective_at_clock":
            changes["valid_from"] = task.evaluation_time
        elif item.clock_boundary == "expires_after_clock":
            changes["valid_until"] = (
                (clock + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            )
        elif item.clock_boundary == "collected_at_clock":
            changes["collected_at"] = task.evaluation_time
        tasks.append(
            evolve(
                task,
                goal=evolve(task.goal, frameworks=item.frameworks),
                sources=tuple(evolve(s, **changes) for s in task.sources),
            )
        )
    return tuple(tasks)
