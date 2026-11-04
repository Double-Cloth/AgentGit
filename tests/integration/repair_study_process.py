"""真实派发提交后直接退出，仅访问父测试建立的隔离 Schema。"""

import os
import re
import sys
import time
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa

from agentgit.config import Settings, database_url
from agentgit.services.history import HistoryService
from agentgit.services.repair_reductions import RepairReductionService
from agentgit.services.repair_studies import RepairStudyService
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
    ledger = RepairStudyService(history, lease_ms=5000)
    lease = ledger.claim("crash-process", repository=UUID(repository))
    assert lease is not None
    reserved = ledger.reserve(lease)
    assert reserved["admitted"]

    def stop(stage):
        if stage == "after_dispatch_commit":
            time.sleep(5.1)
            os._exit(73)

    RepairReductionService(ResearchService(history, Path(root)), fault=stop).dispatch(
        lease, UUID(reserved["trial_id"])
    )
    raise RuntimeError("没有触发预定的进程中断")


if __name__ == "__main__":
    main()
