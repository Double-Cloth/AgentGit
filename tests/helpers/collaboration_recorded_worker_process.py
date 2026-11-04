"""Recorded Worker 实际解释器终止探针，执行客户端构造均记录后拒绝。"""

import json
import os
import re
from pathlib import Path
from threading import Event

import sqlalchemy as sa
from sqlalchemy import create_engine

from agentgit.adapters.collaboration_model import MockCollaborationModel
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema = os.environ["AGENTGIT_RECORD_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    point = os.environ["AGENTGIT_RECORD_POINT"]
    assert point in ("collaboration.recorded.in_transaction", "collaboration.recorded_frame_saved")
    engine = create_engine(
        os.environ["AGENTGIT_RECORD_DATABASE"],
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    history = HistoryService(engine, ObjectStore(Path(os.environ["AGENTGIT_RECORD_OBJECTS"])))
    queue = JobService(
        ResearchService(history, Path(os.environ["AGENTGIT_RECORD_DATA"])), lease_ms=1000
    )

    def forbidden(*args, **kwargs):
        with Path(os.environ["AGENTGIT_RECORD_CALLS"]).open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(pid=os.getpid(), forbidden_client=True)) + "\n")
            f.flush()
            os.fsync(f.fileno())
        raise AssertionError("Recorded 不能构造执行客户端")

    for cls in (MockCollaborationModel, ToolRegistry, ArtifactWorkspace):
        cls.__init__ = forbidden

    def stop(current):
        if current != point:
            return
        if point.endswith("frame_saved"):
            with engine.connect() as c:
                count = c.execute(
                    sa.select(sa.func.count()).select_from(TABLES["collaboration_recorded_steps"])
                ).scalar_one()
            if count < int(os.environ["AGENTGIT_RECORD_MIN_STEPS"]):
                return
        marker = Path(os.environ["AGENTGIT_RECORD_MARKER"])
        with marker.with_suffix(".pending").open("x", encoding="utf-8") as f:
            json.dump(
                dict(
                    pid=os.getpid(),
                    parent_pid=os.getppid(),
                    point=point,
                    schema=schema,
                    source=source_revision(),
                    lock=dependency_lock_digest(),
                ),
                f,
            )
            f.flush()
            os.fsync(f.fileno())
        marker.with_suffix(".pending").replace(marker)
        Event().wait()

    try:
        Worker(queue, fault=stop).run_once()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
