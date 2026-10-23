"""干预只有固定字段白名单，不能修改验收器、测试 split 或权限。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.core.interventions import NewIntervention


def request():
    return {
        "schema_version": 1,
        "source_run_id": str(uuid4()),
        "base_commit": "a" * 64,
        "target_commit_id": "a" * 64,
        "target_event_id": 1,
        "operator": "PROMPT_PATCH",
        "payload": {"kind": "PROMPT_PATCH", "prompt": "核验来源"},
        "precondition": {
            "state_ref": "b" * 64,
            "config_ref": "c" * 64,
            "validator_ref": "d" * 64,
            "occurrence": {
                "logical_step_id": "model-1",
                "call_index": 0,
                "attempt": 1,
                "provider": "llm:mock-research/v1",
                "signature_digest": "e" * 64,
                "request_digest": "f" * 64,
            },
        },
        "effect_policy": "sandbox_or_approval",
        "provenance": {"rationale": "固定测试"},
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("validator", "always_pass"),
        ("fixture_split", "confirm"),
        ("tools", ["host_shell"]),
        ("permissions", ["all"]),
        ("operator", "MODEL_CONFIG_PATCH"),
        ("effect_policy", "test_only"),
    ],
)
def test_unknown_or_escalating_fields_are_rejected(field, value):
    with pytest.raises(ValidationError):
        NewIntervention.model_validate({**request(), field: value})


@pytest.mark.parametrize(
    "field", ["validator", "fixture_split", "model", "tools", "execution_policy"]
)
def test_prompt_payload_cannot_hide_other_changes(field):
    data = request()
    data["payload"][field] = "forbidden"
    with pytest.raises(ValidationError):
        NewIntervention.model_validate(data)


def test_payload_operator_and_effect_policy_must_agree():
    data = request()
    data["operator"] = "TOOL_RESPONSE_PATCH"
    with pytest.raises(ValidationError):
        NewIntervention.model_validate(data)


def test_tool_patch_can_only_reference_recorded_response_and_is_test_only():
    data = request()
    data.update(operator="TOOL_RESPONSE_PATCH", effect_policy="test_only")
    data["payload"] = {"kind": "TOOL_RESPONSE_PATCH", "replacement_call_id": str(uuid4())}
    assert NewIntervention.model_validate(data).payload.test_only
    for extra in (
        {"response": {"ok": True}},
        {"test_only": False},
        {"url": "https://example.invalid"},
    ):
        with pytest.raises(ValidationError):
            NewIntervention.model_validate({**data, "payload": {**data["payload"], **extra}})
