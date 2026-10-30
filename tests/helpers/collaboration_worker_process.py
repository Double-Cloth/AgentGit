"""独立测试解释器：在角色的确切耐久边界等待父进程终止。"""

import hashlib
import json
import os
import re
from pathlib import Path
from threading import Event

from sqlalchemy import create_engine

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def counted_model(path):
    class Counted(MockCollaborationModel):
        def invoke(self, payload):
            # 在真实 invoke 边界记录独立计数，不用意图行冒充实际调用。
            with Path(path).open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(dict(pid=os.getpid(), request=hashlib.sha256(payload).hexdigest()))
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            return super().invoke(payload)

    return Counted()


def main():
    schema = os.environ["AGENTGIT_COLLAB_TEST_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    target = os.environ["AGENTGIT_COLLAB_TEST_POINT"]
    assert target in {
        "collaboration.call_reserved",
        "collaboration.after_call_intent",
        "collaboration.after_saved_result",
        "collaboration.export_ready_saved",
        "collaboration.exchange_projected",
        "collaboration.exchange_committed",
    }

    def wait(point):
        if point != target:
            return
        marker = Path(os.environ["AGENTGIT_COLLAB_TEST_MARKER"])
        with marker.with_suffix(".pending").open("x", encoding="utf-8") as stream:
            json.dump(
                dict(
                    pid=os.getpid(),
                    parent_pid=os.getppid(),
                    point=point,
                    schema=schema,
                    source_revision=source_revision(),
                    lock_digest=dependency_lock_digest(),
                ),
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        marker.with_suffix(".pending").replace(marker)
        Event().wait()

    engine = create_engine(
        os.environ["AGENTGIT_COLLAB_TEST_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(
            engine, ObjectStore(Path(os.environ["AGENTGIT_COLLAB_TEST_OBJECTS"]))
        )
        queue = JobService(
            ResearchService(history, Path(os.environ["AGENTGIT_COLLAB_TEST_DATA"])), lease_ms=1000
        )
        Worker(
            queue,
            fault=wait,
            collaboration_model_factory=lambda role: counted_model(
                os.environ["AGENTGIT_COLLAB_TEST_CALLS"]
            ),
        ).run_once()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
