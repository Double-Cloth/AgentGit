"""独立测试进程：只连接显式 test Schema，在确切持久边界等待父进程终止。"""

import json
import os
import re
import time
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import create_engine

from agentgit.config import database_url
from agentgit.core.canonical import encode
from agentgit.core.merge_proposals import ApproveMergeProposal
from agentgit.core.models import parse_contract
from agentgit.services.environment import source_revision
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.merge_proposals import MergeProposalService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


def main():
    schema = os.environ["AGENTGIT_MERGE_CRASH_SCHEMA"]
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    point = os.environ["AGENTGIT_MERGE_CRASH_POINT"]
    mode = os.environ["AGENTGIT_MERGE_CRASH_MODE"]
    assert mode in ("approve", "run")
    marker = Path(os.environ["AGENTGIT_MERGE_CRASH_MARKER"])

    def wait_at(actual):
        if actual != point:
            return
        pending = marker.with_suffix(".pending")
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(
                dict(
                    pid=os.getpid(),
                    parent_pid=os.getppid(),
                    point=point,
                    mode=mode,
                    source_revision=source_revision(),
                ),
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, marker)
        while True:
            time.sleep(0.1)

    engine = create_engine(
        database_url(SecretStr(os.environ["AGENTGIT_MERGE_CRASH_DATABASE"])),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(
            engine, ObjectStore(Path(os.environ["AGENTGIT_MERGE_CRASH_OBJECTS"])), fault=wait_at
        )
        if mode == "run":
            research = ResearchService(history, Path(os.environ["AGENTGIT_MERGE_CRASH_DATA"]))
            Worker(JobService(research, lease_ms=500)).run_once()
        else:
            data = json.loads(Path(os.environ["AGENTGIT_MERGE_CRASH_REQUEST"]).read_text("utf-8"))
            request = parse_contract(encode(data["request"]), ApproveMergeProposal)
            MergeProposalService(history).approve(
                UUID(data["repository_id"]),
                UUID(data["proposal_id"]),
                request,
                actor=data["actor"],
                key=data["key"],
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
