"""A2A 固定程序与累计账本；不伪造远端完成或取消。"""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.core.a2a import A2ABinding, A2AServer
from agentgit.core.a2a_runs import A2AJobPayload, A2AProgram, A2ARunState
from agentgit.core.canonical import encode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.storage.schema import build_metadata
from agentgit.storage.schema_collaboration_recorded_steps import build_metadata as prior_metadata

ROOT = Path(__file__).parents[1] / "fixtures/a2a/v1"


def program():
    card = json.loads((ROOT / "agent-card.json").read_text("utf-8"))
    body = json.loads((ROOT / "cases.json").read_text("utf-8"))["request"]
    binding = A2ABinding(
        server=A2AServer(
            server_identity="fixture", principal_id="operator", endpoint="http://127.0.0.1:1"
        ),
        card_json=encode(card).decode(),
    )
    return A2AProgram(binding=binding, messages=(encode(body).decode(),))


def test_program_roundtrip_and_schema_do_not_add_tables():
    value = program()
    assert parse_contract(encode(value.model_dump(mode="json")), A2AProgram) == value
    assert len(build_metadata().tables) == len(prior_metadata().tables)
    assert value.binding.server.credential_env == "AGENTGIT_A2A_FIXTURE_OPERATOR_TOKEN"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_polls", 0),
        ("max_polls", 33),
        ("poll_interval_ms", 19),
        ("poll_interval_ms", 1001),
        ("messages", []),
        ("messages", ["{}"]),
        ("replay", {}),
    ],
)
def test_program_rejects_unsupported_scope(field, value):
    body = program().model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        A2AProgram.model_validate_json(encode(body))


def test_duplicate_messages_or_caller_task_ids_are_rejected():
    body = program().model_dump(mode="json")
    body["messages"] *= 2
    with pytest.raises(ValidationError):
        A2AProgram.model_validate_json(encode(body))
    body = program().model_dump(mode="json")
    request = json.loads(body["messages"][0])
    request["message"]["taskId"] = "forged"
    body["messages"][0] = encode(request).decode()
    with pytest.raises(ValidationError):
        A2AProgram.model_validate_json(encode(body))


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tool_calls": 17, "max_steps": 17},
        {"max_tool_calls": 65, "max_steps": 65},
        {"max_steps": 39},
        {"max_files": 8},
        {"max_artifact_bytes": 65536},
        {"max_response_bytes": 32768},
        {"max_wall_ms": 1000},
    ],
)
def test_program_budget_covers_messages_polls_cancel_and_artifacts(changes):
    body = program().model_dump(mode="json")
    body["limits"].update(changes)
    with pytest.raises(ValidationError):
        A2AProgram.model_validate_json(encode(body))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": RunStatus.COMPLETED.value},
        {"remote_state": "TASK_STATE_UNSPECIFIED"},
        {"remote_cancel_status": "confirmed"},
        {"polls": 1},
        {"next_message": 1},
        {"defer_pending": True, "status": RunStatus.PAUSED.value},
        {"usage": {"steps": 1, "tool_calls": 1}},
        {"usage": {"model_calls": 1}},
        {"last_response_ref": "a" * 64},
    ],
)
def test_state_cannot_invent_usage_results_or_cancel(changes):
    body = A2ARunState(program_ref="b" * 64).model_dump(mode="json")
    body.update(copy.deepcopy(changes))
    with pytest.raises(ValidationError):
        A2ARunState.model_validate_json(encode(body))


def test_job_locks_code_and_does_not_accept_recorded_yet():
    value = dict(
        program_ref="a" * 64,
        actor="operator",
        code_revision="source-sha256:" + "b" * 64,
        dependency_lock_digest="c" * 64,
        max_wall_ms=60000,
    )
    A2AJobPayload(**value)
    with pytest.raises(ValidationError):
        A2AJobPayload(**value, replay={"mode": "recorded"})
