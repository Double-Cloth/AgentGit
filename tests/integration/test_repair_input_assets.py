"""三十套新公开输入的构造与隔离，不执行故障或保留集修复评估。"""

from copy import deepcopy

import pytest
import repair_dataset as dataset
from repair_dataset import (
    DATA,
    InputSpecifications,
    Profiles,
    input_ref,
    public_input,
    task_for_case,
)

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, Evidence, evolve
from agentgit.runtime.validation import render_report, report_from_memory, validate_report


def assets():
    return parse_contract((DATA / "cases.json").read_bytes(), InputSpecifications), parse_contract(
        (DATA / "inputs.json").read_bytes(), Profiles
    )


@pytest.mark.parametrize("index", range(30))
def test_public_input_has_new_corpus_and_passes_unchanged_oracle_from_constructed_memory(index):
    specifications, profiles = assets()
    case = specifications.cases[index]
    task = task_for_case(case, profiles)
    assert input_ref(task) == case.public_input_ref
    assert set(s.id for s in task.sources).isdisjoint(Corpus.bundled().sources)
    state = AgentState(
        goal=task.goal,
        working_memory={
            s.id: Evidence(source=s, logical_step_id=f"read-{i}", attempt=1)
            for i, s in enumerate(task.sources)
        },
    )
    report = report_from_memory(state, task.evaluation_time)
    evaluation = validate_report(
        state,
        {
            "sources.json": encode(report.model_dump(mode="json")),
            "report.md": render_report(report).encode(),
        },
        Corpus(task.sources, evaluation_time=task.evaluation_time),
    )
    assert evaluation.passed and not evaluation.errors
    raw = encode(task.model_dump(mode="json"))
    for forbidden in (
        case.id,
        "heldout",
        "declared_repairable",
        case.pattern,
        case.family,
        "unverified",
    ):
        assert forbidden.encode() not in raw
    assert len(report.claims) == 9 and len(report.sources) == 6


def test_labels_and_split_never_change_the_public_task():
    specifications, profiles = assets()
    case = specifications.cases[0]
    changed = evolve(
        case,
        split="heldout",
        family="altered-family",
        category="joint",
        pattern="altered-pattern",
        declared_repairable=False,
    )
    assert task_for_case(case, profiles) == task_for_case(changed, profiles)
    assert len({input_ref(task_for_case(c, profiles)) for c in specifications.cases}) == 30


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate_profile",
        "split",
        "family_alias",
        "category",
        "denominator",
        "fake_gold",
        "oracle_start",
        "expected_passed",
    ],
)
def test_manifest_rejects_missing_cases_family_leakage_and_result_metadata(mutation):
    value = decode((DATA / "cases.json").read_bytes())
    if mutation == "missing":
        value["cases"].pop()
    elif mutation == "duplicate_profile":
        value["cases"][1]["profile"] = value["cases"][0]["profile"]
    elif mutation == "split":
        value["cases"][0]["split"] = "heldout"
    elif mutation == "family_alias":
        value["families"][-1]["patterns"].append(value["families"][0]["patterns"][0])
    elif mutation == "category":
        value["cases"][0]["category"] = "joint"
    elif mutation == "denominator":
        value["cases"][0]["declared_repairable"] = False
    elif mutation == "fake_gold":
        value["label_status"] = "human_approved"
    elif mutation == "oracle_start":
        value["source_rule"] = "known-injection-checkpoint"
    else:
        value["cases"][0]["expected_passed"] = True
    with pytest.raises(AgentGitError):
        parse_contract(encode(value), InputSpecifications)


@pytest.mark.parametrize("mutation", ["profile", "digest", "unknown_profile"])
def test_task_binding_rejects_changed_input_or_missing_catalogue(mutation):
    specifications, profiles = assets()
    case = specifications.cases[0]
    if mutation == "profile":
        case = evolve(case, profile=specifications.cases[1].profile)
    elif mutation == "digest":
        case = evolve(case, public_input_ref="0" * 64)
    else:
        case = evolve(case, profile="missing-profile")
    with pytest.raises(AgentGitError):
        task_for_case(case, profiles)


@pytest.mark.parametrize("clock", ["2025-06-01T00:00:00Z", "2029-01-01T00:00:00Z", "2026-06-01"])
def test_public_baseline_rejects_future_collection_expiry_or_naive_time(clock):
    _, profiles = assets()
    with pytest.raises(AgentGitError):
        public_input(evolve(profiles.profiles[0], evaluation_time=clock))


@pytest.mark.parametrize(
    "mutation",
    ["none", "cases_bytes", "profiles_bytes", "builder_digest", "methods_digest", "partition"],
)
def test_frozen_release_detects_bytes_code_or_partition_change(tmp_path, monkeypatch, mutation):
    original = dataset.DATA
    copied = tmp_path / "v1"
    copied.mkdir()
    for name in ("cases.json", "inputs.json", "inputs-release.json"):
        (copied / name).write_bytes((original / name).read_bytes())
    (copied.parent / "methods-v1.json").write_bytes(
        (original.parent / "methods-v1.json").read_bytes()
    )
    if mutation in ("cases_bytes", "profiles_bytes"):
        name = "cases.json" if mutation == "cases_bytes" else "inputs.json"
        (copied / name).write_bytes((copied / name).read_bytes() + b"\n")
    elif mutation != "none":
        release = deepcopy(decode((copied / "inputs-release.json").read_bytes()))
        if mutation == "partition":
            release["train"].reverse()
        else:
            release["builder_sha256" if mutation == "builder_digest" else "methods_sha256"] = (
                "0" * 64
            )
        (copied / "inputs-release.json").write_bytes(encode(release))
    monkeypatch.setattr(dataset, "DATA", copied)
    if mutation == "none":
        specifications, profiles, release = dataset.load_inputs()
        assert len(specifications.cases) == len(profiles.profiles) == 30
        assert release.fault_runtime_ref is None and release.heldout_evaluation == "not_started"
    else:
        with pytest.raises(AgentGitError):
            dataset.load_inputs()
