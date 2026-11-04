"""只连接测试自有 Schema，在明确耐久边界等待父测试终止。"""

import json
import os
import re
import time
from pathlib import Path

from sqlalchemy import create_engine

from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema = os.environ["AGENTGIT_MCP_TEST_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    target = os.environ["AGENTGIT_MCP_TEST_POINT"]
    assert target in {"mcp.after_intent", "mcp.after_result", "mcp.after_observation"}

    def wait(point):
        if point != target:
            return
        Path(os.environ["AGENTGIT_MCP_TEST_MARKER"]).write_text(
            json.dumps(
                {"pid": os.getpid(), "parent_pid": os.getppid(), "point": point, "schema": schema}
            ),
            encoding="utf-8",
        )
        while True:
            time.sleep(0.1)

    engine = create_engine(
        os.environ["AGENTGIT_MCP_TEST_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(engine, ObjectStore(Path(os.environ["AGENTGIT_MCP_TEST_OBJECTS"])))
        queue = JobService(
            ResearchService(history, Path(os.environ["AGENTGIT_MCP_TEST_DATA"])), lease_ms=1000
        )
        Worker(queue, fault=wait).run_once()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
