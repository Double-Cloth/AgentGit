"""原子准备请求必须显式提供四项不同回归来源，不能上传结果代替条件。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.canonical import encode
from agentgit.core.repair_pools import NewPreparedRepairSearch


def body():
    return dict(
        schema_version=1,
        search=dict(
            schema_version=1,
            failed_run_id=str(uuid4()),
            base_commit="a" * 64,
            validator_ref="b" * 64,
            targets=[dict(schema_version=1, event_id=1)],
        ),
        regression=[
            dict(schema_version=1, source_run_id=str(uuid4()), from_commit="c" * 64)
            for _ in range(4)
        ],
    )


def test_exact_four_origins_and_search_options_survive_canonical_roundtrip():
    request = body()
    request["search"]["fixture_scope"] = "stochastic_fixture"
    parsed = NewPreparedRepairSearch.model_validate_json(encode(request))
    assert len(parsed.regression) == 4 and parsed.search.fixture_scope == "stochastic_fixture"
    assert (
        NewPreparedRepairSearch.model_validate_json(encode(parsed.model_dump(mode="json")))
        == parsed
    )


def test_missing_reused_or_forged_result_conditions_are_rejected():
    good = body()
    invalid = [
        {**good, "regression": good["regression"][:3]},
        {**good, "regression": good["regression"] + [good["regression"][0]]},
        {**good, "regression": [good["regression"][0]] * 4},
        {**good, "passed": True},
    ]
    forged = deepcopy(good)
    forged["regression"][0]["passed"] = True
    invalid.append(forged)
    for request in invalid:
        with pytest.raises(ValidationError):
            NewPreparedRepairSearch.model_validate_json(encode(request))
