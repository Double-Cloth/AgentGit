"""MCP 实际任务的数据库关系与 CAS 原字节导出。"""

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
def archive_mcp_run(history, server, request, tmp_path):
    yield
    destination = os.environ.get("AGENTGIT_MCP_RUN_EVIDENCE_DIR")
    if destination is None:
        return
    config, counts, _ = server
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
        "branch_forks",
        "events",
        "external_calls",
        "approvals",
        "idempotency_keys",
    )
    with history.engine.connect() as connection:
        tables = {
            name: [dict(row) for row in connection.execute(sa.select(TABLES[name])).mappings()]
            for name in names
        }
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        migration = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    objects = []
    for row in tables["objects"]:
        payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
        objects.append(
            {
                "id": row["id"],
                "type": row["object_type"],
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "base64": base64.b64encode(payload).decode("ascii"),
            }
        )
    packet = {
        "kind": "mcp_raw_runs/v1",
        "test": request.node.nodeid,
        "source_revision": source_revision(),
        "lock_digest": dependency_lock_digest(),
        "schema": schema,
        "migration": migration,
        "server": config.model_dump(mode="json"),
        "counts": counts(),
        "tables": tables,
        "objects": objects,
    }
    proof = tmp_path / "worker-death.json"
    if proof.is_file():
        packet["process_death"] = json.loads(proof.read_text("utf-8"))
    folder = Path(destination)
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16] + ".json"
    with (folder / name).open("x", encoding="utf-8") as handle:
        json.dump(packet, handle, ensure_ascii=False, indent=2, default=str)
