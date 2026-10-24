"""独立进程在真实派发提交后退出；只连接父测试创建的隔离 Schema。"""

import os
import re
import sys
import time
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from agentgit.config import Settings, database_url
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.history import HistoryService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore


def main():
    schema, objects, root, repository = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = sa.create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )

    def stop(stage):
        if stage == "after_dispatch_commit":
            time.sleep(5.1)
            os._exit(73)

    history = HistoryService(engine, ObjectStore(Path(objects)))
    RepairSearchWorker(ResearchService(history, Path(root)), lease_ms=5000, fault=stop).run_once(
        repository=UUID(repository),
    )
    raise RuntimeError("没有触发预定的进程中断")


if __name__ == "__main__":
    main()
