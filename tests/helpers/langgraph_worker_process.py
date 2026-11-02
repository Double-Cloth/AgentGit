"""仅连接测试自有 Schema，在真实图 Worker 的确切耐久边界等待终止。"""

import json
import os
import re
from pathlib import Path
from threading import Event

from sqlalchemy import create_engine, select

from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
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
        "langgraph.after_fork_seed",
        "langgraph.after_fork_writes",
        "langgraph.after_fork_checkpoint",
        "langgraph.after_recorded_seed",
        "langgraph.after_recorded_call",
        "langgraph.after_recorded_writes",
        "langgraph.after_recorded_checkpoint",
        "langgraph.after_recorded_pause",
    }

    def wait(point):
        if point != target:
            return
        path = Path(os.environ["AGENTGIT_GRAPH_TEST_MARKER"])
        pending = path.with_suffix(".pending")
        with history.engine.connect() as connection:
            active_run = connection.execute(
                select(TABLES["jobs"].c.run_id).where(TABLES["jobs"].c.status == "running")
            ).scalar_one()
        pending.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "parent_pid": os.getppid(),
                    "point": point,
                    "schema": schema,
                    "source_revision": source_revision(),
                    "lock_digest": dependency_lock_digest(),
                    "run_id": str(active_run),
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
        queue = JobService(ResearchService(history, Path(os.environ["AGENTGIT_GRAPH_TEST_DATA"])))
        Worker(queue, fault=wait).run_once()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
