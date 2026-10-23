"""Hybrid 只读精确匹配、冻结 miss 行为与 synthetic 标记。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.hybrid import FrozenHybridPolicy, HybridCall
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass
from agentgit.core.research import SavedCallOutcome, evolve
from agentgit.experiments.hybrid import HybridSelector
from agentgit.storage.objects import ObjectStore


@pytest.fixture
def selected(tmp_path):
    objects, corpus = ObjectStore(tmp_path / "objects"), Corpus.bundled()
    request = encode({"schema_version": 1, "frameworks": ["Aster"]})
    response = encode({"schema_version": 1, "source_ids": list(corpus.search(("Aster",)))})
    request_ref = objects.put(ObjectType.BLOB, request).id
    response_ref = objects.put(ObjectType.BLOB, response).id
    occurrence = CallOccurrence(
        logical_step_id="search",
        call_index=0,
        attempt=1,
        provider="tool:corpus_search/v1",
        signature_digest="a" * 64,
        request_digest=request_ref,
    )
    call = HybridCall(
        source_call_id=str(uuid4()),
        intent=CallIntent(
            occurrence=occurrence,
            effect=EffectClass.READ_ONLY,
            request_ref=request_ref,
            checkpoint="b" * 64,
            max_response_bytes=16384,
        ),
        saved=SavedCallOutcome(
            response_ref=response_ref, response_bytes=len(response), complete=True
        ),
    )
    policy = FrozenHybridPolicy(
        source_run_id=str(uuid4()),
        from_commit="b" * 64,
        until_commit="c" * 64,
        validator_ref="d" * 64,
        corpus_digest=corpus.digest(),
        miss_policy="stop",
        calls=(call,),
    )
    return objects, corpus, policy, occurrence, request, response


def test_exact_recording_is_returned_with_provenance_and_models_are_fresh(selected):
    objects, corpus, policy, occurrence, request, response = selected
    selector = HybridSelector(policy, corpus, objects.get)
    result = selector.resolve(occurrence, EffectClass.READ_ONLY, request, 16384)
    assert result.payload == response and result.error is None
    assert result.provenance.kind == "recorded" and result.provenance.matched
    assert result.provenance.selected_call_id == policy.calls[0].source_call_id
    assert (
        selector.resolve(
            evolve(occurrence, provider="llm:mock-research/v1"),
            EffectClass.READ_ONLY,
            request,
            16384,
        )
        is None
    )
    assert (
        selector.resolve(evolve(occurrence, call_index=1), EffectClass.READ_ONLY, request, 16384)
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "tool:source_read/v1"),
        ("signature_digest", "f" * 64),
    ],
)
def test_signature_or_provider_miss_never_uses_old_output(selected, field, value):
    objects, corpus, policy, occurrence, request, _ = selected
    for miss in ("stop", "fixture"):
        selector = HybridSelector(evolve(policy, miss_policy=miss), corpus, objects.get)
        with pytest.raises(AgentGitError) as caught:
            selector.resolve(
                evolve(occurrence, **{field: value}), EffectClass.READ_ONLY, request, 16384
            )
        assert caught.value.code == ErrorCode.REPLAY_MISMATCH


def test_changed_arguments_stop_or_generate_new_fixture_result(selected):
    objects, corpus, policy, occurrence, _, old_response = selected
    request = encode({"schema_version": 1, "frameworks": ["Birch"]})
    changed = evolve(occurrence, request_digest=object_digest(ObjectType.BLOB, request))
    with pytest.raises(AgentGitError) as caught:
        HybridSelector(policy, corpus, objects.get).resolve(
            changed, EffectClass.READ_ONLY, request, 16384
        )
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    result = HybridSelector(evolve(policy, miss_policy="fixture"), corpus, objects.get).resolve(
        changed, EffectClass.READ_ONLY, request, 16384
    )
    assert result.payload != old_response
    assert result.payload == encode(
        {"schema_version": 1, "source_ids": list(corpus.search(("Birch",)))}
    )
    assert result.provenance.kind == "fixture" and not result.provenance.matched


def test_synthetic_patch_is_only_applied_on_exact_match(selected):
    objects, corpus, policy, occurrence, request, _ = selected
    replacement = encode({"schema_version": 1, "source_ids": []})
    call = evolve(
        policy.calls[0],
        replacement_response_ref=objects.put(ObjectType.BLOB, replacement).id,
        intervention_id=str(uuid4()),
    )
    selector = HybridSelector(
        evolve(policy, calls=(call,), miss_policy="fixture"), corpus, objects.get
    )
    result = selector.resolve(occurrence, EffectClass.READ_ONLY, request, 16384)
    assert result.payload == replacement
    assert result.provenance.kind == "synthetic" and result.provenance.test_only
    assert result.provenance.intervention_id == call.intervention_id
    changed_request = encode({"schema_version": 1, "frameworks": ["Cedar"]})
    changed = evolve(occurrence, request_digest=object_digest(ObjectType.BLOB, changed_request))
    miss = selector.resolve(changed, EffectClass.READ_ONLY, changed_request, 16384)
    assert miss.provenance.kind == "fixture" and miss.provenance.intervention_id is None
    assert miss.payload != replacement


def test_corruption_is_not_hidden_by_fixture_miss_policy(selected):
    objects, corpus, policy, occurrence, request, _ = selected
    objects.path_for(policy.calls[0].saved.response_ref).write_bytes(b"bad")
    with pytest.raises(AgentGitError) as caught:
        HybridSelector(evolve(policy, miss_policy="fixture"), corpus, objects.get).resolve(
            occurrence, EffectClass.READ_ONLY, request, 16384
        )
    assert caught.value.code == ErrorCode.OBJECT_CORRUPT


def test_simulator_and_policy_do_not_alias_mutable_inputs(selected):
    objects, corpus, policy, occurrence, request, _ = selected
    failed = evolve(
        policy.calls[0],
        saved=evolve(policy.calls[0].saved, error={"code": "tool_empty", "message": "原始错误"}),
    )
    original_policy = evolve(policy, calls=(failed,), miss_policy="fixture")
    selector = HybridSelector(original_policy, corpus, objects.get)
    original_policy.calls[0].saved.error["message"] = "调用方修改"
    corpus.sources.clear()
    exact = selector.resolve(occurrence, EffectClass.READ_ONLY, request, 16384)
    assert exact.error.message == "原始错误"
    changed_request = encode({"schema_version": 1, "frameworks": ["Birch"]})
    changed = evolve(occurrence, request_digest=object_digest(ObjectType.BLOB, changed_request))
    result = selector.resolve(changed, EffectClass.READ_ONLY, changed_request, 16384)
    assert result.provenance.kind == "fixture"
    assert result.payload == encode(
        {"schema_version": 1, "source_ids": list(Corpus.bundled().search(("Birch",)))}
    )


def test_changed_effect_or_small_budget_cannot_reuse_recorded_bytes(selected):
    objects, corpus, policy, occurrence, request, _ = selected
    with pytest.raises(AgentGitError):
        HybridSelector(evolve(policy, miss_policy="fixture"), corpus, objects.get).resolve(
            occurrence, EffectClass.IDEMPOTENT_WRITE, request, 16384
        )
    with pytest.raises(AgentGitError):
        HybridSelector(policy, corpus, objects.get).resolve(
            occurrence, EffectClass.READ_ONLY, request, 1
        )


def test_missing_selected_dependency_cannot_be_counted_as_complete(selected):
    objects, corpus, policy, *_ = selected
    with pytest.raises(AgentGitError) as caught:
        HybridSelector(policy, corpus, objects.get).require_reached(())
    assert caught.value.code == ErrorCode.RECORDING_MISSING


@pytest.mark.parametrize(
    "provider", ["llm:mock-research/v1", "tool:artifact_write/v1", "tool:terminal_exec/v1"]
)
def test_model_and_write_recordings_are_not_selectable(selected, provider):
    _, _, policy, occurrence, *_ = selected
    with pytest.raises(ValidationError):
        evolve(
            policy.calls[0],
            intent=evolve(policy.calls[0].intent, occurrence=evolve(occurrence, provider=provider)),
        )
