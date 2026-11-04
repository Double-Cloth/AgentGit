"""冻结协议及标签隔离的负例，不执行定位排序或使用保留集调参。"""

import hashlib
from copy import deepcopy

import pytest
from diagnosis_dataset import (
    DATA,
    Dataset,
    DatasetRelease,
    Faults,
    load_dataset,
    verify_release,
)
from diagnosis_evidence import verify_repository_evidence

from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import RunStatus, parse_contract
from agentgit.core.research import AgentState, ModelRequest, ResearchGoal, evolve


@pytest.mark.parametrize("mutation", ["missing", "partition", "operation", "human_claim"])
def test_dataset_rejects_missing_cases_changed_partition_unknown_recipe_and_fake_review(mutation):
    value = decode((DATA / "cases.json").read_bytes())
    if mutation == "missing":
        value["cases"].pop()
    elif mutation == "partition":
        value["cases"][12]["split"] = "train"
    elif mutation == "operation":
        value["cases"][0]["operation"] = "execute_host_command"
    else:
        value["cases"][0]["annotation"]["review_status"] = "human_approved"
    with pytest.raises(AgentGitError) as error:
        parse_contract(encode(value), Dataset)
    assert error.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize("mutation", ["specification", "fixture", "partition"])
def test_frozen_digest_or_partition_change_requires_new_version(mutation):
    dataset = load_dataset()
    payload = (DATA / "cases.json").read_bytes()
    fixture = (DATA.parents[2] / "integration/diagnosis_dataset.py").read_bytes()
    release = DatasetRelease(
        cases_sha256=hashlib.sha256(payload).hexdigest(),
        fixture_sha256=hashlib.sha256(fixture).hexdigest(),
        train=tuple(case.id for case in dataset.cases[:12]),
        heldout=tuple(case.id for case in dataset.cases[12:]),
        annotation_status="pending_human",
        ranking_status="not_implemented",
        frozen_at_utc="2026-10-24T00:00:00Z",
        dataset_id="diagnosis/v1",
    )
    verify_release(dataset, payload, fixture, release)
    if mutation == "specification":
        payload += b"\n"
    elif mutation == "fixture":
        fixture += b"\n"
    else:
        release = evolve(release, heldout=tuple(reversed(release.heldout)))
    with pytest.raises(AgentGitError) as error:
        verify_release(dataset, payload, fixture, release)
    assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_expected_labels_and_split_do_not_change_injected_model_response():
    case = next(case for case in load_dataset().cases if case.operation == "omit_sources")
    alternative = evolve(
        case,
        split="heldout",
        expected_error=ErrorCode.MODEL_ERROR,
        annotation=evolve(
            case.annotation,
            injection="arbitrary:injection",
            first_visible="arbitrary:visible",
            repairable_decisions=("arbitrary:candidate",),
        ),
    )
    payload = encode(
        ModelRequest(
            prompt="固定测试请求",
            phase="plan",
            state=AgentState(
                goal=ResearchGoal(text="比较三个 fixture 框架"), status=RunStatus.PLANNING
            ),
        ).model_dump(mode="json")
    )
    assert Faults(case, True).model(payload) == Faults(alternative, True).model(payload)


@pytest.mark.parametrize("mutation", ["none", "digest", "foreign_project"])
def test_offline_evidence_checks_original_bytes_and_project_boundary(mutation):
    identity = object_digest(ObjectType.BLOB, b"")
    result = {
        "repository_id": "local-fixture",
        "graph": {"nodes": []},
        "reference_graph": {"nodes": []},
    }
    bundle = {
        "test_only": True,
        "repository_id": "local-fixture",
        "raw_bytes": 0,
        "objects": {identity: {"object_type": "blob", "byte_length": 0, "base64": ""}},
        "indexes": {
            "events": [],
            "object_refs": [{"repository_id": "local-fixture", "object_id": identity}],
        },
    }
    verify_repository_evidence(bundle, result)
    changed = deepcopy(bundle)
    if mutation == "digest":
        changed["objects"][identity]["base64"] = "YQ=="
    elif mutation == "foreign_project":
        changed["indexes"]["object_refs"][0]["repository_id"] = "other-fixture"
    else:
        return
    with pytest.raises(AssertionError):
        verify_repository_evidence(changed, result)
