"""独立测试控制进程，等待父进程在提交前或提交后终止实际解释器。"""

import json
import os
import re
from pathlib import Path
from threading import Event
from uuid import UUID

from sqlalchemy import create_engine

from agentgit.core.collaboration_controls import CollaborationControlRequest
from agentgit.services.collaboration_controls import CollaborationControlService
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore


def main():
    schema = os.environ["AGENTGIT_COLLAB_CONTROL_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    target = os.environ["AGENTGIT_COLLAB_CONTROL_POINT"]
    assert target in ("collaboration.control_projected", "collaboration.control_committed")

    def stop(point):
        if point != target:
            return
        marker = Path(os.environ["AGENTGIT_COLLAB_CONTROL_MARKER"])
        with marker.with_suffix(".pending").open("x", encoding="utf-8") as stream:
            json.dump(
                dict(
                    pid=os.getpid(),
                    parent_pid=os.getppid(),
                    point=point,
                    schema=schema,
                    source=source_revision(),
                    lock=dependency_lock_digest(),
                ),
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        marker.with_suffix(".pending").replace(marker)
        Event().wait()

    engine = create_engine(
        os.environ["AGENTGIT_COLLAB_CONTROL_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(
            engine, ObjectStore(Path(os.environ["AGENTGIT_COLLAB_CONTROL_OBJECTS"])), fault=stop
        )
        queue = JobService(
            ResearchService(history, Path(os.environ["AGENTGIT_COLLAB_CONTROL_DATA"]))
        )
        CollaborationControlService(queue).request(
            UUID(os.environ["AGENTGIT_COLLAB_CONTROL_REPO"]),
            UUID(os.environ["AGENTGIT_COLLAB_CONTROL_GROUP"]),
            CollaborationControlRequest(action="cancel", expected_revision=0),
            actor="collaboration-operator",
            key="process-control",
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
