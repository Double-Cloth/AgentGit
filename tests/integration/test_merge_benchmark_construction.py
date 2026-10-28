"""三十例输入准备控制；本套件尚不输出正式效果或基线成绩。"""

from uuid import UUID

import pytest
from merge_benchmark_cases import build_case, specs
from merge_proposal_cases import archive, history_fingerprint

from agentgit.core.errors import AgentGitError
from agentgit.core.merge import MergeOrigin
from agentgit.services.merge_validation import preview_research_merge
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("spec", specs(), ids=lambda item: item["id"])
def test_actual_merge_input_construction(history, tmp_path, spec):
    repo, _research, _manager, payload, details = build_case(history, tmp_path, spec)
    assert payload.ours.head != payload.theirs.head
    # 独立查询双侧真实祖先，不能用测试预期替代已发布历史。
    versions = VersionService(history)
    assert details["base"] in versions.ancestors(repo, payload.ours.head)
    assert details["base"] in versions.ancestors(repo, payload.theirs.head)
    assert UUID(payload.ours.source_run_id) != UUID(payload.theirs.source_run_id)
    archive(history, repo, tmp_path, spec["id"] + "/source", details)
    before = history_fingerprint(history, repo)
    try:
        preview = preview_research_merge(
            history,
            repo,
            MergeOrigin(run_id=payload.ours.source_run_id, commit_id=payload.ours.head),
            MergeOrigin(run_id=payload.theirs.source_run_id, commit_id=payload.theirs.head),
            payload.validator_ref,
        )
    except AgentGitError as error:
        assert error.code.value in spec["errors"]
        result = dict(error=error.code.value, message=str(error))
    else:
        validation = preview.validation
        assert validation.validated == (spec["expected"] == "accept")
        assert set(spec["kinds"]) <= {item.kind.value for item in validation.conflicts}
        assert validation.origins[0].commit_id == details["base"]
        result = validation.model_dump(mode="json")
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        spec["id"] + "/final",
        dict(
            construction=details,
            preview=result,
            result="construction-not-formal-results",
            proposal=payload.model_dump(mode="json"),
        ),
    )
