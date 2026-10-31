"""只访问测试自有 Schema，在真实耐久边界等待父测试终止解释器。"""

import json
import os
import re
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine

from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema = os.environ["AGENTGIT_A2A_TEST_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    target = os.environ["AGENTGIT_A2A_TEST_POINT"]
    assert target in {
        "a2a.after_intent",
        "a2a.after_result",
        "a2a.after_observation",
        "a2a.after_defer",
    }

    def wait(point):
        if point != target:
            return
        marker = Path(os.environ["AGENTGIT_A2A_TEST_MARKER"])
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                dict(
                    pid=os.getpid(),
                    parent_pid=os.getppid(),
                    point=point,
                    schema=schema,
                    source=source_revision(),
                    lock=dependency_lock_digest(),
                    interpreter=sys.executable,
                )
            ),
            encoding="utf-8",
        )
        temporary.replace(marker)
        while True:
            release = os.environ.get("AGENTGIT_A2A_TEST_RELEASE")
            if release is not None and Path(release).exists():
                return
            time.sleep(0.1)

    engine = create_engine(
        os.environ["AGENTGIT_A2A_TEST_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(engine, ObjectStore(Path(os.environ["AGENTGIT_A2A_TEST_OBJECTS"])))
        queue = JobService(
            ResearchService(history, Path(os.environ["AGENTGIT_A2A_TEST_DATA"])), lease_ms=1000
        )
        result = Worker(queue, fault=wait).run_once()
        destination = os.environ.get("AGENTGIT_A2A_TEST_RESULT")
        if destination is not None:
            Path(destination).write_text(json.dumps(result), encoding="utf-8")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
