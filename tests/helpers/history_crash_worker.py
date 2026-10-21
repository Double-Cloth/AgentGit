"""由故障注入用例启动的独立进程，只操作隔离测试库与 pytest 临时 CAS。"""

import os
import re
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from agentgit.config import Settings, database_url
from agentgit.core.models import EnvironmentManifest
from agentgit.services.history import ArtifactInput, EventInput, HistoryService
from agentgit.storage.objects import ObjectStore


def main():
    root, schema, repo, run, point, input_ref = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )

    def crash(current):
        if current == point or (point == "after_publish" and current == "before_transaction"):
            os._exit(73)

    objects = ObjectStore(Path(root), fault=crash if point == "after_temp" else None)
    service = HistoryService(engine, objects, fault=crash)
    branch = service.branch(UUID(repo))
    environment = EnvironmentManifest(
        runtime_version="0.1.0",
        python_version="3.12",
        code_revision="s2-crash",
        dependency_lock_digest="a" * 64,
        model_signature="mock/v1",
        prompt_digest="b" * 64,
        input_ref=input_ref,
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    service.commit(
        UUID(repo),
        UUID(run),
        expected_head=branch["head_commit_id"],
        expected_version=branch["version"],
        state={"value": 1},
        environment=environment,
        events=(EventInput("state.updated", "step", {"value": 1}),),
        artifacts=(ArtifactInput("report.md", b"complete"),),
    )
    raise RuntimeError("故障点未触发")


if __name__ == "__main__":
    main()
