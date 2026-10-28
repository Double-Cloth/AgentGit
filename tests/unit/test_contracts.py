"""S1-T04：Schema 不做静默转换。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import (
    CallOccurrence,
    CommitManifest,
    Contract,
    EnvironmentManifest,
    EventRange,
    parse_contract,
)


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": 2},
        {"schema_version": "1"},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"x": 1},
    ],
)
def test_schema_rejects_unknown(data):
    with pytest.raises(ValidationError):
        Contract.model_validate(data)


def test_contract_is_frozen():
    contract = Contract()
    with pytest.raises(ValidationError):
        contract.schema_version = 1


def test_stable_contract_errors():
    for raw in (b"{}", b'{"schema_version":2}', b'{"schema_version":true}'):
        with pytest.raises(AgentGitError) as caught:
            parse_contract(raw, Contract)
        assert caught.value.code == ErrorCode.UNSUPPORTED_SCHEMA
    with pytest.raises(AgentGitError) as caught:
        parse_contract(b'{"schema_version":1,"secret":"private-value"}', Contract)
    assert caught.value.code == ErrorCode.VALIDATION
    assert "private-value" not in str(caught.value)


def test_manifest_schemas_match_frozen_golden_vectors():
    vectors = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/canonical/golden.json").read_text(
            encoding="utf-8"
        )
    )
    models = {
        "root_commit": CommitManifest,
        "environment": EnvironmentManifest,
        "call_occurrence": CallOccurrence,
    }
    for vector in vectors:
        if vector["name"] in models:
            parse_contract(bytes.fromhex(vector["canonical_hex"]), models[vector["name"]])


def test_ranges_parents_and_metadata():
    with pytest.raises(ValidationError):
        EventRange(first_seq=2, last_seq=1)
    value = {
        "schema_version": 1,
        "repository_id": "00000000-0000-4000-8000-000000000001",
        "parents": [],
        "state_ref": "a" * 64,
        "environment_ref": "b" * 64,
        "event_manifest_ref": "c" * 64,
        "event_range": {"first_seq": 1, "last_seq": 2},
    }
    assert CommitManifest.model_validate_json(json.dumps(value)).parents == ()
    assert CommitManifest.model_validate_json(
        json.dumps({**value, "parents": ["a" * 64, "b" * 64]})
    ).parents == ("a" * 64, "b" * 64)
    with pytest.raises(ValidationError):
        CommitManifest.model_validate_json(
            json.dumps({**value, "parents": ["a" * 64, "b" * 64, "c" * 64]})
        )
    with pytest.raises(ValidationError):
        CommitManifest.model_validate_json(json.dumps({**value, "created_at": "now"}))
    with pytest.raises(ValidationError):
        CommitManifest.model_validate_json(json.dumps({**value, "parents": ["a" * 64] * 2}))


def test_occurrence_keeps_repeated_calls_distinct():
    data = {
        "logical_step_id": "read",
        "call_index": 0,
        "attempt": 1,
        "provider": "source_read/v1",
        "signature_digest": "a" * 64,
        "request_digest": "b" * 64,
    }
    first = CallOccurrence(**data)
    second = CallOccurrence(**{**data, "call_index": 1})
    assert first.request_digest == second.request_digest
    assert first != second
    with pytest.raises(ValidationError):
        CallOccurrence(**{**data, "attempt": 0})
