"""独立真实 HTTP 进程；只允许 pytest 隔离库与显式回环监听。"""

import json
import re
import socket
import sys
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine

from agentgit.api.main import create_app
from agentgit.config import Settings, database_url
from agentgit.services.history import HistoryService
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore


def main():
    schema, root, objects, ready = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        manager = ManagementService(
            ResearchService(HistoryService(engine, ObjectStore(Path(objects))), Path(root)),
            actor=settings.operator_id,
        )
        temporary = Path(ready + ".tmp")
        temporary.write_text(json.dumps({"port": listener.getsockname()[1]}), encoding="utf-8")
        temporary.replace(ready)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings, management=manager), access_log=False, log_level="critical"
            )
        )
        server.run(sockets=[listener])
    finally:
        listener.close()
        engine.dispose()


if __name__ == "__main__":
    main()
