"""派发提交后直接退出；仅连接父测试建立的隔离 Schema。"""

import os
import re
import sys
import time
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from agentgit.config import Settings, database_url
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
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
    history = HistoryService(engine, ObjectStore(Path(objects)))

    def fault(stage):
        if stage == "after_dispatch_commit":
            time.sleep(5.1)
            os._exit(73)

    RepairConfirmationWorker(
        ResearchService(history, Path(root)), lease_ms=5000, fault=fault
    ).run_once(repository=UUID(repository))
    raise RuntimeError("未触发预定的验证控制器退出")


if __name__ == "__main__":
    main()
