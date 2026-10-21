"""Recorded 公共提交契约固定范围和新分支，不接受干预字段。"""

import pytest

from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.versions import RecordedReplay


@pytest.mark.parametrize("extra", ["overrides", "budget", "config", "prompt", "model"])
def test_recorded_submission_rejects_every_intervention_field(extra):
    value = {
        "schema_version": 1,
        "mode": "recorded",
        "source_run_id": "00000000-0000-0000-0000-000000000001",
        "from_commit": "a" * 64,
        "until_commit": "b" * 64,
        "branch_name": "recorded",
        extra: {},
    }
    with pytest.raises(AgentGitError) as error:
        parse_contract(encode(value), RecordedReplay)
    assert error.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize("name", [" leading", "trailing ", "line\nbreak", ""])
def test_recorded_submission_uses_same_visible_branch_name_rules(name):
    with pytest.raises(AgentGitError) as error:
        parse_contract(
            encode(
                {
                    "schema_version": 1,
                    "source_run_id": "00000000-0000-0000-0000-000000000001",
                    "from_commit": "a" * 64,
                    "until_commit": "b" * 64,
                    "branch_name": name,
                }
            ),
            RecordedReplay,
        )
    assert error.value.code == ErrorCode.VALIDATION
