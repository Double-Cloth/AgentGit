"""固定图桌面联调：复用独立 API/Worker 启动器，不增加产品提交接口。"""

import base64
import hashlib
import json
import os
import re
import runpy
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

import mcp_backend as mcp
import repair_backend as base
import sqlalchemy as sa
from real_backend import source_fingerprint

from agentgit.adapters.mcp import MCPClient
from agentgit.config import Settings
from agentgit.core.canonical import ObjectType
from agentgit.core.langgraph import LangGraphDefinition, LangGraphProgram
from agentgit.core.langgraph_recorded import NewLangGraphRecorded
from agentgit.core.langgraph_runs import NewLangGraphFork
from agentgit.core.mcp import MCPArguments
from agentgit.core.models import EffectClass
from agentgit.core.research import BudgetLimits
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.langgraph_runs import LangGraphRunService
from agentgit.services.management import public


def worker_main(schema, root):
    history = base.history_for(schema, root)
    queue = base.JobService(base.ResearchService(history, root / "seed" / "research"))

    def gate(point):
        if (
            point != "langgraph.after_fetch_checkpoint"
            or not (root / "graph-boundary").exists()
            or (root / "graph-gate-release").exists()
        ):
            return
        (root / "graph-gate-ready").write_text(point, encoding="utf-8")
        while not (root / "graph-gate-release").exists() and not (root / "worker-stop").exists():
            time.sleep(0.03)

    worker = base.Worker(queue, fault=gate)
    (root / "worker-ready").write_text("ready")
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists():
                time.sleep(0.03)
                continue
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def seed(history, root):
    frozen = mcp.frozen()
    (root / "graph-frozen.json").write_text(json.dumps(frozen), encoding="utf-8")
    if mcp.STDIO is not None:
        server = mcp.STDIO[0].model_copy(update={"server_identity": "s12-graph-stdio"})
    else:
        # 既有可信 fixture 启动及计数逻辑保持，绑定效果在图程序中固定为只读。
        ready = root / "mcp-ready.json"
        import subprocess

        from agentgit.core.mcp import MCPServer

        mcp.ECHO = subprocess.Popen(
            [sys.executable, str(base.ROOT / "tests/helpers/mcp_server.py"), str(ready)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert mcp.ECHO.poll() is None and time.monotonic() < deadline
            time.sleep(0.02)
        server = MCPServer(
            server_identity="s12-graph-http",
            endpoint=json.loads(ready.read_text("utf-8"))["endpoint"],
        )
    boundary = os.environ.get("AGENTGIT_GRAPH_NATIVE_BOUNDARY") == "1"
    if boundary:
        (root / "graph-boundary").write_text("fetch")
    program = LangGraphProgram(
        binding=MCPClient(server).bind("echo", EffectClass.READ_ONLY),
        request=MCPArguments(arguments={"value": "graph-fixture-value"}),
        review_note="桌面来源已核对",
        graph=LangGraphDefinition(pause_before=() if boundary else ("review",)),
        limits=BudgetLimits(
            max_steps=3,
            max_model_calls=1,
            max_tool_calls=1,
            max_replans=0,
            max_wall_ms=300000,
            max_response_bytes=65536,
            max_files=2,
            max_artifact_bytes=262144,
        ),
    )
    repo = history.create_repository("LangGraph 桌面真实任务")
    service = LangGraphRunService(
        base.JobService(base.ResearchService(history, root / "seed" / "research"))
    )
    receipt = service.submit(
        repo,
        program,
        actor=Settings.from_env().operator_id,
        key="graph-native-source",
        branch_name="main",
        expected_head=None,
        expected_version=0,
    )
    assert frozen == mcp.frozen()
    return dict(
        repository=str(repo),
        run_id=receipt.run_id,
        job_id=receipt.job_id,
        server=server.model_dump(mode="json"),
        boundary=boundary,
    )


def extra_command(name, request, history, root, seeded, worker):
    assert (
        worker.poll() is None
        and json.loads((root / "graph-frozen.json").read_text("utf-8")) == mcp.frozen()
    )
    repo = UUID(seeded["repository"])
    source = UUID(seeded["run_id"])
    queue = base.JobService(base.ResearchService(history, root / "seed" / "research"))
    service = LangGraphRunService(queue)
    if name == "graph_gate":
        if request["enabled"]:
            deadline = time.monotonic() + 15
            while not (root / "graph-gate-ready").exists():
                assert worker.poll() is None and time.monotonic() < deadline
                time.sleep(0.03)
        else:
            (root / "graph-gate-release").write_text("release")
        return dict(enabled=request["enabled"])
    with history.engine.connect() as connection:
        links = base.TABLES["run_commits"]
        source_commits = list(
            connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == source)
                .order_by(links.c.run_order)
            ).scalars()
        )
        source_row = dict(history._run(connection, repo, source))
    if name == "graph_fork":
        safe = next(
            cid
            for cid in source_commits
            if history.restore(repo, cid).data.get("native_next") == ["review"]
            and history.restore(repo, cid).data.get("native_operation") == "checkpoint"
        )
        return service.fork(
            repo,
            NewLangGraphFork(
                source_run_id=str(source),
                from_commit=safe,
                name="native-note-fork",
                expected_version=0,
                review_note="桌面独立备注",
            ),
            actor=Settings.from_env().operator_id,
            key="native-note-fork",
        ).model_dump(mode="json")
    if name == "graph_recorded":
        return service.recorded(
            repo,
            NewLangGraphRecorded(
                source_run_id=str(source),
                from_commit=source_commits[0],
                to_commit=source_row["checkpoint_commit_id"],
                name="native-recorded",
                expected_version=0,
            ),
            actor=Settings.from_env().operator_id,
            key="native-recorded",
        ).model_dump(mode="json")
    assert name == "graph_inspect"
    run = UUID(request.get("run", str(source)))
    manager = base.ManagementService(queue.research, actor=Settings.from_env().operator_id)
    result = dict(
        run=public(manager.run(repo, run)),
        counts=mcp.echo_counts(seeded["server"]),
        source_fingerprint=source_fingerprint(history, repo, source),
        source_branch=public(history.branch(repo)),
        gated=(root / "graph-gate-ready").exists(),
    )
    if request.get("archive"):
        label = request["label"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,60}", label)
        with history.engine.connect() as connection:
            tables = {
                name: [
                    dict(row) for row in connection.execute(sa.select(base.TABLES[name])).mappings()
                ]
                for name in (
                    "objects",
                    "object_refs",
                    "repositories",
                    "runs",
                    "jobs",
                    "branches",
                    "branch_forks",
                    "commits",
                    "commit_parents",
                    "run_commits",
                    "snapshots",
                    "artifacts",
                    "events",
                    "external_calls",
                    "idempotency_keys",
                )
            }
        objects = []
        for row in tables["objects"]:
            raw = history.objects.get(row["id"], ObjectType(row["object_type"]))
            objects.append(
                dict(
                    id=row["id"],
                    type=row["object_type"],
                    sha256=hashlib.sha256(raw).hexdigest(),
                    base64=base64.b64encode(raw).decode("ascii"),
                )
            )
        packet = dict(
            kind="langgraph_worker_raw/v1",
            test="desktop/langgraph/" + label,
            source_revision=source_revision(),
            lock_digest=dependency_lock_digest(),
            tables=tables,
            objects=objects,
            counts=result["counts"],
        )
        if mcp.STDIO is not None:
            approved = mcp.STDIO[1]
            packet["stdio"] = dict(
                server=seeded["server"],
                approved_program=approved.model_dump(mode="json"),
                script_base64=base64.b64encode(Path(approved.script).read_bytes()).decode("ascii"),
                interpreter_sha256=hashlib.sha256(
                    Path(approved.interpreter).read_bytes()
                ).hexdigest(),
            )
        target = root / f"langgraph-{label}-evidence.json"
        with target.open("x", encoding="utf-8") as stream:
            json.dump(packet, stream, default=str, ensure_ascii=False, indent=2)
        result["evidence"] = str(target)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        try:
            if os.environ.get("AGENTGIT_MCP_NATIVE_TRANSPORT") == "stdio":

                class Environment:
                    @staticmethod
                    def setenv(key, value):
                        os.environ[key] = value

                helper = runpy.run_path(str(base.ROOT / "tests/helpers/mcp_stdio_fixture.py"))
                with tempfile.TemporaryDirectory(prefix="agentgit-graph-stdio-") as directory:
                    mcp.STDIO = (
                        *helper["prepare"](Path(directory), Environment()),
                        helper["exited"],
                    )
                    base.main(seed_case=seed, extra_command=extra_command)
            else:
                base.main(seed_case=seed, extra_command=extra_command)
        finally:
            if mcp.ECHO is not None:
                mcp.ECHO.terminate()
                mcp.ECHO.wait(timeout=10)
