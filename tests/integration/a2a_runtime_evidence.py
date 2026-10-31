"""A2A Live 的实际队列、状态、审批、产物及 CAS 原字节归档。"""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.history import TABLES


@pytest.fixture(autouse=True)
def archive_a2a_run(history, server, request, tmp_path):
    yield
    destination = os.environ.get("AGENTGIT_A2A_RUN_PROOF_DIR")
    if destination is None:
        return
    names = (
        "objects",
        "object_refs",
        "repositories",
        "runs",
        "jobs",
        "branches",
        "commits",
        "commit_parents",
        "run_commits",
        "snapshots",
        "artifacts",
        "events",
        "external_calls",
        "approvals",
        "idempotency_keys",
    )
    with history.engine.connect() as connection:
        tables = {
            name: [dict(r) for r in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in names
        }
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        migration = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    objects = []
    for row in tables["objects"]:
        raw = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            dict(
                id=row["id"],
                type=row["object_type"],
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                base64=base64.b64encode(raw).decode(),
            )
        )
    packet = dict(
        kind="a2a_runtime_raw/v1",
        test=request.node.nodeid,
        source=source_revision(),
        lock=dependency_lock_digest(),
        schema=schema,
        migration=migration,
        server=server[2](),
        tables=tables,
        objects=objects,
    )
    proof = tmp_path / "process-death.json"
    if proof.exists():
        packet["process_death"] = json.loads(proof.read_text("utf-8"))
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24] + ".json")).open(
        "x", encoding="utf-8"
    ) as stream:
        json.dump(packet, stream, ensure_ascii=False, indent=2, default=str)
