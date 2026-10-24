"""提交耐久意图后直接退出，不执行清理或归还控制租约。"""

import json
import os
import sys
from uuid import UUID

from agentgit.config import Settings
from agentgit.services.history import HistoryService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore

settings = Settings.from_env()
settings.assert_test_isolation()
engine = make_engine(settings, testing=True)
history = HistoryService(engine, ObjectStore(settings.test_data_root / "objects"))
ledger = RepairJobService(history, lease_ms=500)
lease = ledger.claim("abrupt-process", repository=UUID(sys.argv[1]))
assert lease is not None
intent = ledger.reserve(lease)
print(json.dumps({"lease": lease.model_dump(mode="json"), "intent": intent}), flush=True)
os._exit(73)
