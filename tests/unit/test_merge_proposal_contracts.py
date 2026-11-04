"""合并提案的公开契约不能注入审计状态、权限、验证或执行。"""

from uuid import UUID

import pytest
from pydantic import ValidationError

from agentgit.adapters.mock import research_plan
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError
from agentgit.core.merge_proposals import (
    MergeArtifactDocument,
    MergeBranchPin,
    MergeEdits,
    NewMergeProposal,
)
from agentgit.core.models import parse_contract
from agentgit.core.versions import ArtifactPatch


def pin(number, name):
    return MergeBranchPin(
        name=name, source_run_id=str(UUID(int=number)), head=f"{number:064x}", version=0
    )


@pytest.mark.parametrize("other", [pin(1, "theirs"), pin(2, "ours")])
def test_branch_and_head_identity_cannot_be_reused_as_a_two_parent_proposal(other):
    with pytest.raises(ValidationError):
        NewMergeProposal(ours=pin(1, "ours"), theirs=other, validator_ref="a" * 64)


@pytest.mark.parametrize("value", [" ../bad ", "bad\nname", ""])
def test_branch_pin_requires_visible_exact_name(value):
    with pytest.raises(ValidationError):
        pin(1, value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("usage", {}),
        ("effects", []),
        ("validated", True),
        ("tools", ["shell"]),
        ("state", {}),
        ("actor", "forged"),
    ],
)
def test_edits_cannot_include_runtime_or_approval_authority(field, value):
    with pytest.raises(AgentGitError):
        parse_contract(encode(dict(schema_version=1, **{field: value})), MergeEdits)


def test_plan_replacement_requires_explicit_intent_and_clear_is_explicit():
    with pytest.raises(ValidationError):
        MergeEdits(plan=research_plan())
    assert MergeEdits(replace_plan=True).plan is None


@pytest.mark.parametrize(
    "patches",
    [
        (ArtifactPatch(path="script.py", text="print(1)"),),
        (ArtifactPatch(path="report.md", content_b64="YQ=="),),
        (ArtifactPatch(path="report.md", text="text", media_type="application/json"),),
        (
            ArtifactPatch(path="report.md", text="first"),
            ArtifactPatch(path="report.md", text="second"),
        ),
        (ArtifactPatch(path="report.md", text="x" * (1024 * 1024 + 1)),),
    ],
)
def test_artifact_edits_are_bounded_utf8_and_typed(patches):
    with pytest.raises(ValidationError):
        MergeEdits(artifacts=patches)


def test_conflicted_artifact_cannot_hide_an_usable_body():
    with pytest.raises(ValidationError):
        MergeArtifactDocument(
            path="report.md",
            media_type="text/markdown",
            status="conflict",
            text="unsafe",
            evidence_refs=(),
        )
