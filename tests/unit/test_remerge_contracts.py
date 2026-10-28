"""批准来源不能冒充 Run 用量；旧编码和显式来源绑定保持精确。"""

from dataclasses import replace
from uuid import UUID

import pytest
from pydantic import ValidationError
from test_merge_state import incoming

from agentgit.adapters.corpus import Corpus
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import ApprovedMergeOrigin, MergeConstraints, MergeSourceUsage
from agentgit.core.merge_proposals import MergeBranchPin, MergeProposalManifest, NewMergeProposal
from agentgit.core.research import BudgetUsage
from agentgit.merge.state import merge_state
from agentgit.services.merge_inputs import load_state_input


def approved_origin():
    return ApprovedMergeOrigin(approval_id=str(UUID(int=10)), commit_id="a" * 64)


def test_nonexecuted_source_has_no_fabricated_zero_usage_or_run_id():
    origin = approved_origin()
    usage = MergeSourceUsage(origin=origin, usage=None, execution="not_executed")
    assert usage.model_dump(mode="json")["usage"] is None
    assert "run_id" not in origin.model_dump(mode="json")
    with pytest.raises(ValidationError):
        ApprovedMergeOrigin.model_validate_json(
            '{"approval_id":"'
            + str(UUID(int=10))
            + '","commit_id":"'
            + "a" * 64
            + '","run_id":"'
            + str(UUID(int=1))
            + '"}'
        )


@pytest.mark.parametrize(
    "usage,execution", [(BudgetUsage(), "not_executed"), (BudgetUsage(), None), (None, None)]
)
def test_approved_source_rejects_execution_claims(usage, execution):
    with pytest.raises(ValidationError):
        MergeSourceUsage(origin=approved_origin(), usage=usage, execution=execution)


def test_actual_run_usage_remains_required_and_legacy_bytes_have_no_new_field():
    original = incoming(1).origin
    item = MergeSourceUsage(origin=original, usage=BudgetUsage(model_calls=5))
    assert set(item.model_dump(mode="json")) == {"schema_version", "origin", "usage"}
    assert "kind" not in original.model_dump(mode="json")
    for usage, execution in ((None, None), (BudgetUsage(), "not_executed")):
        with pytest.raises(ValidationError):
            MergeSourceUsage(origin=original, usage=usage, execution=execution)


def test_kernel_uses_approved_constraints_and_keeps_original_read_proofs():
    base, ours, theirs = (incoming(number) for number in (1, 2, 3))
    constraints = MergeConstraints(
        tools=base.config.tools,
        limits=base.config.limits,
        evaluation_time=Corpus.bundled().evaluation_time,
    )
    projected = replace(base, origin=approved_origin(), config=constraints)
    candidate = merge_state(projected, ours, theirs, Corpus.bundled())
    assert candidate.proposed_state is not None
    state = candidate.proposed_state
    assert state.source_usage[0].usage is None and state.source_usage[0].execution == "not_executed"
    assert all(proof.origin.run_id for proof in state.read_proofs)


def request():
    return NewMergeProposal(
        ours=MergeBranchPin(name="ours", source_run_id=str(UUID(int=2)), head="2" * 64, version=0),
        theirs=MergeBranchPin(
            name="theirs", source_run_id=str(UUID(int=3)), head="3" * 64, version=0
        ),
        validator_ref="b" * 64,
    )


def test_explicit_base_run_requires_commit_and_manifest_agreement():
    normal = request()
    assert "selected_base_run_id" not in normal.model_dump(mode="json")
    with pytest.raises(ValidationError):
        NewMergeProposal.model_validate(
            {**normal.model_dump(), "selected_base_run_id": str(UUID(int=1))}
        )
    selected = NewMergeProposal.model_validate(
        {**normal.model_dump(), "selected_base": "1" * 64, "selected_base_run_id": str(UUID(int=1))}
    )
    origins = (incoming(1).origin, incoming(2).origin, incoming(3).origin)
    # 与请求相同的真实内容标识，不靠展示排序选择另一个 Run。
    origins = tuple(
        origin.model_copy(update={"commit_id": str(number) * 64})
        for number, origin in enumerate(origins, 1)
    )
    assert MergeProposalManifest(
        request=selected, origins=origins
    ).request.selected_base_run_id == str(UUID(int=1))
    with pytest.raises(ValidationError):
        MergeProposalManifest(
            request=selected,
            origins=(origins[0].model_copy(update={"run_id": str(UUID(int=9))}), *origins[1:]),
        )


def test_recursive_source_cycle_is_rejected_before_any_storage_access(monkeypatch):
    def recurse(history, repository, origin, corpus, context):
        return load_state_input(history, repository, origin, corpus, _context=context)

    monkeypatch.setattr("agentgit.services.merge_inputs._load_approved_input", recurse)
    with pytest.raises(AgentGitError) as error:
        load_state_input(None, UUID(int=1), approved_origin(), Corpus.bundled())
    assert error.value.code == ErrorCode.OBJECT_CORRUPT
