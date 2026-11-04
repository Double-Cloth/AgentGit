"""实际结果未知的写入即使没有消息记录，也必须阻断状态合并。"""

import json
import os
from pathlib import Path

import pytest
from test_approvals import effect_server as effect_server
from test_merge_research_validation import archive, cas, counts
from test_reconciliation import unknown as unknown

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType
from agentgit.core.merge import ConflictKind, MergeOrigin
from agentgit.core.models import CallIntent, parse_contract
from agentgit.merge.state import merge_state
from agentgit.services.environment import source_revision
from agentgit.services.merge_inputs import load_state_input

pytestmark = pytest.mark.integration


def test_unknown_effect_absent_from_messages_blocks_without_resending(history, unknown, tmp_path):
    _queue, repo, run, _job, call, _worker, effect_counts, _adapter = unknown
    intent = parse_contract(history.objects.get(call["intent_ref"], ObjectType.CALL), CallIntent)
    assert intent.checkpoint is not None
    # 读取调用前的真实检查点；后续未知结果不会出现在这个快照的 messages 中。
    origin = MergeOrigin(run_id=str(run), commit_id=intent.checkpoint)
    revision = source_revision()
    before = counts(history)
    objects = cas(history)
    external_before = effect_counts()
    archive(history, repo, tmp_path, "unknown/source", None, before, {"main": history.branch(repo)})
    loaded = load_state_input(history, repo, origin, Corpus.bundled())
    matching = [item for item in loaded.effects if item.call_id == str(call["id"])]
    assert len(matching) == 1 and matching[0].status == "outcome_unknown"
    assert matching[0].result_ref is None
    assert not any(item.provider == matching[0].provider for item in loaded.state.messages)
    result = merge_state(loaded, loaded, loaded, Corpus.bundled())
    assert result.proposed_state is None
    assert ConflictKind.EFFECT_CONFLICT in {item.kind for item in result.conflicts}
    assert counts(history) == before and cas(history) == objects
    assert effect_counts() == external_before and source_revision() == revision
    root = Path(os.environ.get("AGENTGIT_MERGE_EVIDENCE", str(tmp_path / "evidence")))
    (root / "unknown/result.json").write_text(
        json.dumps(
            dict(
                source_revision=revision,
                origin=origin.model_dump(mode="json"),
                effects=[item.model_dump(mode="json") for item in loaded.effects],
                conflicts=[item.model_dump(mode="json") for item in result.conflicts],
                external_before=external_before,
                external_after=effect_counts(),
            ),
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
