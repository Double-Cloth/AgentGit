"""MCP 桌面真实任务：独立 HTTP Server、API、Worker 及自有测试 Schema。"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
from uuid import UUID

import repair_backend as base
import sqlalchemy as sa
from merge_backend import frozen_files, worker_main

from agentgit.adapters.mcp import MCPClient
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.mcp import MCPArguments, MCPServer
from agentgit.core.mcp_runs import MCPProgram
from agentgit.core.models import EffectClass
from agentgit.core.research import BudgetLimits
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService, public
from agentgit.services.mcp_runs import MCPRunService

ECHO = None


def frozen():
    values = frozen_files()
    for name in ("tests/helpers/mcp_server.py", "tests/fixtures/mcp/v1/tools.json"):
        values[name] = hashlib.sha256((base.ROOT / name).read_bytes()).hexdigest()
    return values


def seed(history, root):
    global ECHO
    manifest = frozen()
    (root / "mcp-frozen.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    ready = root / "mcp-ready.json"
    ECHO = subprocess.Popen(
        [sys.executable, str(base.ROOT / "tests/helpers/mcp_server.py"), str(ready)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert ECHO.poll() is None and time.monotonic() < deadline
        time.sleep(0.02)
    endpoint = json.loads(ready.read_text("utf-8"))["endpoint"]
    server = MCPServer(server_identity="s12-desktop-fixture", endpoint=endpoint)
    binding = MCPClient(server).bind("echo", EffectClass.NON_IDEMPOTENT_WRITE)
    limits = BudgetLimits(
        max_steps=1,
        max_model_calls=1,
        max_tool_calls=1,
        max_replans=0,
        max_wall_ms=300000,
        max_response_bytes=65536,
        max_artifact_bytes=65536,
        max_files=1,
    )
    program = MCPProgram(
        binding=binding,
        request=MCPArguments(arguments={"value": "native-mcp-result"}),
        limits=limits,
    )
    repo = history.create_repository("MCP 桌面真实任务")
    service = MCPRunService(JobService(base.ResearchService(history, root / "seed" / "research")))
    submitted = service.submit(
        repo,
        program,
        actor=Settings.from_env().operator_id,
        key="native-mcp-once",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    assert manifest == frozen()
    return dict(
        repository=str(repo),
        run_id=submitted.run_id,
        job_id=submitted.job_id,
        server=server.model_dump(mode="json"),
        frozen=str(root / "mcp-frozen.json"),
    )


def echo_counts(server):
    endpoint = server["endpoint"].removesuffix("/mcp")
    with build_opener(ProxyHandler({})).open(endpoint + "/state", timeout=2) as response:
        return json.loads(response.read(8192))


def extra_command(name, request, history, root, seeded, worker):
    assert name == "mcp_inspect" and worker.poll() is None
    assert json.loads((root / "mcp-frozen.json").read_text("utf-8")) == frozen()
    repo, run = UUID(seeded["repository"]), UUID(seeded["run_id"])
    manager = ManagementService(
        base.ResearchService(history, root / "seed" / "research"),
        actor=Settings.from_env().operator_id,
    )
    view = manager.run(repo, run)
    counts = echo_counts(seeded["server"])
    with history.engine.connect() as connection:
        tables = {
            name: [dict(row) for row in connection.execute(sa.select(base.TABLES[name])).mappings()]
            for name in (
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
        }
        migration = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    model_calls = sum(row["provider"].startswith("llm:") for row in tables["external_calls"])
    result = dict(
        run=public(view),
        counts=counts,
        model_calls=model_calls,
        approvals=[public(row) for row in tables["approvals"]],
    )
    if request.get("archive"):
        label = request.get("label", "final")
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,60}", label)
        objects = []
        for row in tables["objects"]:
            payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
            objects.append(
                dict(
                    id=row["id"],
                    type=row["object_type"],
                    bytes=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                    base64=base64.b64encode(payload).decode("ascii"),
                )
            )
        packet = dict(
            kind="mcp_raw_runs/v1",
            test="desktop/mcp-real/v1",
            server=seeded["server"],
            counts=counts,
            source_revision=source_revision(),
            lock_digest=dependency_lock_digest(),
            migration=migration,
            tables=tables,
            objects=objects,
        )
        target = root / f"mcp-{label}-evidence.json"
        with target.open("x", encoding="utf-8") as handle:
            json.dump(packet, handle, default=str, ensure_ascii=False, indent=2)
        result["evidence"] = str(target)
    return result


if __name__ == "__main__":
    os.environ.pop("AGENTGIT_MCP_S12_DESKTOP_FIXTURE_TOKEN", None)
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        try:
            base.main(seed_case=seed, extra_command=extra_command)
        finally:
            if ECHO is not None:
                ECHO.terminate()
                ECHO.wait(timeout=10)
