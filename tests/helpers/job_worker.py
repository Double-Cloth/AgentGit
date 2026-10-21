"""独立进程中的真实 Worker；只允许 pytest 隔离 Schema 与临时数据。"""

import json
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine

from agentgit.adapters.mock import MockResearchModel
from agentgit.config import Settings, database_url
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema, root, objects, marker, point, number = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )

    def record(value):
        with Path(marker).open("a", encoding="utf-8") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    class CountingModel(MockResearchModel):
        def invoke(self, payload):
            record("model")
            if point == "during_model":
                os._exit(73)
            return super().invoke(payload)

    class CountingWorker(Worker):
        def _attach(self, lease):
            session, origins, remaining = super()._attach(lease)
            invoke = session.tools.invoke

            def counted(*args, **kwargs):
                record("tool:" + args[0])
                return invoke(*args, **kwargs)

            session.tools.invoke = counted
            return session, origins, remaining

    count = 0

    def fault(current):
        nonlocal count
        if current == point:
            count += 1
            if count == int(number):
                os._exit(73)

    try:
        history = HistoryService(engine, ObjectStore(Path(objects)))
        queue = JobService(ResearchService(history, Path(root)), lease_ms=1500)
        print(
            json.dumps(CountingWorker(queue, model_factory=CountingModel, fault=fault).run_once())
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
