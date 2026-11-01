"""当前工程版本不能复用旧研究协议、创建任务或覆盖历史结果。"""

import json

import diagnosis_effect_execution_v3 as diagnosis
import merge_benchmark_release as merge_release
import pytest
import repair_benchmark_release as repair_release
import repair_dataset_execution as repair
import sqlalchemy as sa
import test_merge_benchmark_evaluation as merge
from diagnosis_dataset import load_dataset

from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for name, table in TABLES.items()
        }


def forbidden(*args, **kwargs):
    raise AssertionError("旧研究协议被拒绝前不得执行、写CAS或构造来源")


@pytest.mark.parametrize("study", ["diagnosis", "repair", "merge"])
def test_current_revision_rejects_historical_study_before_any_execution(
    history, tmp_path, monkeypatch, study
):
    path = {
        "diagnosis": diagnosis.PROTOCOL,
        "repair": repair_release.RELEASE_PATH,
        "merge": merge_release.RELEASE,
    }[study]
    original = path.read_bytes()
    protocol = json.loads(original)
    historical = protocol.get("source_revision", protocol.get("code_revision"))
    assert historical != source_revision()
    before = counts(history)
    inventory = dict(history.objects.inventory())
    with monkeypatch.context() as isolated:
        isolated.setattr(history.objects, "put", forbidden)
        isolated.setattr(MockResearchModel, "invoke", forbidden)
        isolated.setattr(ToolRegistry, "invoke", forbidden)
        isolated.setattr(Worker, "run_once", forbidden)
        if study == "diagnosis":
            isolated.setattr(diagnosis, "run_case", forbidden)
            with pytest.raises(ValueError, match="效果协议、条件或代码变化"):
                diagnosis.execute_case(history, tmp_path / "rejected", load_dataset().cases[0])
        elif study == "repair":
            isolated.setattr(repair, "_execute_case", forbidden)
            with pytest.raises(ValueError, match="旧 release 不能执行或覆盖"):
                repair.execute_frozen_case(
                    history,
                    tmp_path / "rejected",
                    "R01",
                    repair_release.OUTPUT / "cases/R01",
                )
        else:
            isolated.setenv("AGENTGIT_MERGE_BENCHMARK_STAGE", "formal")
            isolated.setattr(merge, "build_case", forbidden)
            with pytest.raises(ValueError, match="产品源码已变化"):
                merge.test_merge_benchmark_methods_and_approved_execution(
                    history, tmp_path / "rejected", merge.specs()[0], isolated
                )
    assert counts(history) == before and dict(history.objects.inventory()) == inventory
    assert not (tmp_path / "rejected").exists()
    assert path.read_bytes() == original
