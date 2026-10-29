"""仅连接测试自有 Schema，在真实图 Worker 的确切耐久边界等待终止。"""

import json
import os
import re
from pathlib import Path
from threading import Event

from sqlalchemy import create_engine

from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema = os.environ["AGENTGIT_GRAPH_TEST_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    target = os.environ["AGENTGIT_GRAPH_TEST_POINT"]
    assert target in {
        "langgraph.after_intent",
        "langgraph.after_result",
        "langgraph.after_fetch_writes",
        "langgraph.after_fetch_checkpoint",
    }

    def wait(point):
        if point != target:
            return
        path = Path(os.environ["AGENTGIT_GRAPH_TEST_MARKER"])
        pending = path.with_suffix(".pending")
        pending.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "point": point,
                    "schema": schema,
                    "source_revision": source_revision(),
                    "lock_digest": dependency_lock_digest(),
                }
            ),
            encoding="utf-8",
        )
        pending.replace(path)
        Event().wait()

    engine = create_engine(
        os.environ["AGENTGIT_GRAPH_TEST_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(
            engine, ObjectStore(Path(os.environ["AGENTGIT_GRAPH_TEST_OBJECTS"]))
        )
        queue = JobService(
            ResearchService(history, Path(os.environ["AGENTGIT_GRAPH_TEST_DATA"])), lease_ms=1000
        )
        Worker(queue, fault=wait).run_once()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
