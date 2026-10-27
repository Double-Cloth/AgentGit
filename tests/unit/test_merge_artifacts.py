"""实际三方内容、证据摘要、有限 Schema 和不可静默覆盖的产物候选。"""

from copy import deepcopy

import pytest

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, decode, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import ConflictKind
from agentgit.core.research import AgentState, Evidence, ResearchGoal
from agentgit.core.snapshots import ArtifactEntry
from agentgit.merge.artifacts import MAX_FILE_BYTES, ArtifactVersion, merge_artifact
from agentgit.runtime.validation import report_from_memory


def version(text, path="report.md", media=None):
    content = text.encode("utf-8") if isinstance(text, str) else text
    return ArtifactVersion(
        ArtifactEntry(
            path=path,
            media_type=media or ("application/json" if path == "sources.json" else "text/markdown"),
            object_ref=object_digest(ObjectType.BLOB, content),
        ),
        content,
    )


def md(base, ours, theirs):
    return merge_artifact(
        "report.md", *(version(text) if text is not None else None for text in (base, ours, theirs))
    )


def report():
    memory = {
        key: Evidence(source=source, logical_step_id="read-" + key, attempt=1)
        for key, source in Corpus.bundled().sources.items()
    }
    state = AgentState(goal=ResearchGoal(text="比较三个框架"), working_memory=memory)
    return report_from_memory(state, Corpus.bundled().evaluation_time).model_dump(mode="json")


def json_merge(base, ours, theirs):
    return merge_artifact(
        "sources.json",
        *(
            version(encode(value), "sources.json") if value is not None else None
            for value in (base, ours, theirs)
        ),
    )


def test_different_sections_merge_exact_bytes_with_provenance_and_pending_validation():
    base = "# 报告\r\n## A\r\na\r\n## B\r\nb\r\n"
    result = md(base, base.replace("a\r\n", "ours\r\n"), base.replace("b\r\n", "theirs\r\n"))
    assert (
        result.status == "merged"
        and result.content
        == base.replace("a\r\n", "ours\r\n").replace("b\r\n", "theirs\r\n").encode()
    )
    assert len(result.evidence_refs) == 3 and result.conflicts == ()
    assert set(result.validation_pending) == {"state", "provenance", "task_constraints", "approval"}


@pytest.mark.parametrize(
    "ours,theirs", [("# P\n## A\nx\n", "# P\n## A\ny\n"), ("# P\n", "# P\n## A\ny\n")]
)
def test_same_section_different_edits_or_delete_edit_is_blocking(ours, theirs):
    result = md("# P\n## A\na\n", ours, theirs)
    assert result.status == "conflict" and result.content is None
    assert result.conflicts[0].kind == ConflictKind.WRITE_WRITE
    assert result.conflicts[0].evidence_refs == result.evidence_refs


@pytest.mark.parametrize(
    "base,ours,theirs,status",
    [
        (None, "# 新建\n", None, "merged"),
        (None, "# 新建\n", "# 新建\n", "merged"),
        ("# 旧\n", None, "# 旧\n", "deleted"),
        ("# 旧\n", None, "# 新\n", "conflict"),
        (None, "# A\n", "# B\n", "conflict"),
    ],
)
def test_file_creation_deletion_and_edit_are_distinct(base, ours, theirs, status):
    result = md(base, ours, theirs)
    assert result.status == status
    assert (result.content is None) == (status != "merged")


def test_fenced_headings_are_body_and_independent_new_sections_have_stable_order():
    base = "# P\n## A\n```md\n## example\n```\n"
    result = md(base, base + "## C\nc\n", base + "## B\nb\n")
    assert result.content == (base + "## B\nb\n## C\nc\n").encode()
    assert md(base, base, base).content == base.encode()


@pytest.mark.parametrize(
    "text", ["## A\nx\n## A\ny\n", "## \n", "```\nbody", "\ufeff# BOM", "bad\x00data", b"\xff"]
)
def test_unsupported_markdown_is_rejected_even_if_both_heads_agree(text):
    with pytest.raises(AgentGitError) as error:
        md(text, text, text)
    assert error.value.code == ErrorCode.UNSUPPORTED_SCHEMA


def test_existing_section_reorder_is_not_silently_normalized():
    with pytest.raises(AgentGitError):
        md("## A\na\n## B\nb\n", "## B\nb\n## A\na\n", "## A\na\n## B\nb\n")
    assert md(None, "## A\na\n## B\nb\n", "## B\nb\n## A\na\n").status == "conflict"


def test_independent_append_cannot_concatenate_a_heading_into_an_unterminated_body():
    result = md("## A\na\n", "## A\nchanged", "## A\na\n## B\nb\n")
    assert result.status == "conflict" and result.content is None
    assert result.conflicts[0].paths == ("/artifacts/report.md/@layout",)


def test_typed_claims_merge_by_identity_but_still_require_hard_validation():
    base = report()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["claims"][0]["value"] = "左侧待核验"
    theirs["claims"][1]["value"] = "右侧待核验"
    result = json_merge(base, ours, theirs)
    assert result.status == "merged" and result.content is not None
    values = {item["value"] for item in decode(result.content)["claims"]}
    assert {"左侧待核验", "右侧待核验"} <= values
    assert "provenance" in result.validation_pending


def test_same_fact_different_values_and_time_changes_are_explicit_conflicts():
    base = report()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["claims"][0]["value"] = "左"
    theirs["claims"][0]["value"] = "右"
    result = json_merge(base, ours, theirs)
    assert result.content is None and result.conflicts[0].kind == ConflictKind.FACT_CONTRADICTION
    theirs = deepcopy(base)
    theirs["evaluation_time"] = "2030-01-01T00:00:00Z"
    result = json_merge(base, base, theirs)
    assert result.content is None and result.conflicts[0].kind == ConflictKind.TEMPORAL_MISMATCH


def test_evidence_is_never_combined_from_different_fields_of_one_source():
    base = report()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["sources"][0]["source"]["body"] = "左侧正文"
    theirs["sources"][0]["source"]["url"] = "fixture://右侧"
    result = json_merge(base, ours, theirs)
    assert result.status == "conflict" and result.content is None
    assert result.conflicts[0].kind == ConflictKind.WRITE_WRITE


@pytest.mark.parametrize("collection", ["claims", "sources"])
def test_duplicate_entity_identity_is_rejected_even_for_a_new_single_sided_file(collection):
    value = report()
    value[collection].append(deepcopy(value[collection][0]))
    with pytest.raises(AgentGitError) as error:
        json_merge(None, value, None)
    assert error.value.code == ErrorCode.VALIDATION


@pytest.mark.parametrize(
    "path,content,media",
    [
        ("script.py", b"print(1)", "text/x-python"),
        ("arbitrary.json", b"{}", "application/json"),
        ("report.md", b"a", "text/plain"),
        ("sources.json", b"null", "application/json"),
    ],
)
def test_unknown_types_and_schema_do_not_use_a_generic_json_or_text_fallback(path, content, media):
    value = version(content, path, media)
    with pytest.raises(AgentGitError):
        merge_artifact(path, value, value, value)


def test_tampered_blob_path_and_oversized_file_fail_closed():
    value = version("# 合法\n")
    with pytest.raises(AgentGitError) as error:
        merge_artifact("report.md", value, ArtifactVersion(value.entry, b"tampered"), value)
    assert error.value.code == ErrorCode.OBJECT_CORRUPT
    with pytest.raises(AgentGitError):
        merge_artifact("../report.md", value, value, value)
    with pytest.raises(AgentGitError) as error:
        md("x" * (MAX_FILE_BYTES + 1), None, None)
    assert error.value.code == ErrorCode.OUTPUT_LIMIT
