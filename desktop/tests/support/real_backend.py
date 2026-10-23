"""桌面联调专用真实设施：独立测试 Schema、HTTP API 与独立 Worker 进程。"""

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid4

import sqlalchemy as sa
import uvicorn
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from starlette.responses import JSONResponse

from agentgit.adapters.execution.engine import DockerEngine, EngineError
from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.model_http import HTTPReply, TransportFailure
from agentgit.adapters.openai import OpenAIResearchModel
from agentgit.api.main import create_app
from agentgit.config import Settings, database_url
from agentgit.core.canonical import decode, encode
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.management import ManagementService, public
from agentgit.services.research import ResearchService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker

ROOT = Path(__file__).resolve().parents[3]


def isolated_engine(schema):
    assert re.fullmatch("test_desktop_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    return sa.create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )


def worker_main(schema, root):
    engine = isolated_engine(schema)
    history = HistoryService(engine, ObjectStore(root / "objects"))
    research = ResearchService(history, root / "data")
    original = DockerEngine.local

    def local(cls):
        if (root / "recorded-guard").exists():
            with (root / "engine-guard-hits").open("a") as output:
                output.write("hit\n")
            raise AssertionError("Recorded 不得初始化执行客户端")
        return original()

    DockerEngine.local = classmethod(local)

    def model():
        if (root / "recorded-guard").exists():
            with (root / "model-guard-hits").open("a") as output:
                output.write("hit\n")
            raise AssertionError("Recorded 不得创建模型")
        return MockResearchModel()

    class Responses:
        def post(self, path, body, **_):
            # 所有供应商请求都在独立测试 Worker 内终止，绝不接触真实账户。
            counting = path.endswith("input_tokens")
            with (root / "provider-calls").open("a") as output:
                output.write("count\n" if counting else "generate\n")
            if counting:
                return HTTPReply(
                    200, encode({"object": "response.input_tokens", "input_tokens": 100})
                )
            mode = (
                (root / "provider-case").read_text()
                if (root / "provider-case").exists()
                else "normal"
            )
            if mode == "unknown":
                raise TransportFailure(sent=True)
            request = json.loads(json.loads(body)["input"])
            answer = decode(MockResearchModel().invoke(encode(request)))
            structured = {
                key: answer.get(key) for key in ("kind", "plan", "node_key", "tool", "summary")
            }
            structured["arguments_json"] = (
                json.dumps(answer["arguments"], ensure_ascii=False)
                if "arguments" in answer
                else None
            )
            return HTTPReply(
                200,
                json.dumps(
                    {
                        "id": "resp_desktop_fixture",
                        "object": "response",
                        "model": "gpt-5.4-mini-2026-03-17",
                        "service_tier": "default",
                        "status": "completed",
                        "error": None,
                        "incomplete_details": None,
                        "usage": None
                        if mode == "missing_usage"
                        else {
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                            "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 5},
                        },
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [
                                    {"type": "output_text", "text": json.dumps(structured)}
                                ],
                            }
                        ],
                    }
                ).encode(),
            )

    class FixtureWorker(Worker):
        def _model(self, config):
            offline = model()
            if config.openai is None:
                return offline
            os.environ["OPENAI_API_KEY"] = "sk-public-desktop-contract-fixture-only"
            return OpenAIResearchModel(
                config.openai,
                cost_limit_micro_usd=config.limits.max_cost_micro_usd,
                _transport=Responses(),
            )

    try:
        worker = FixtureWorker(ManagementService(research).jobs, name="desktop-real-worker")
        (root / "worker-ready").write_text("ready")
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists():
                (root / "worker-gated").write_text("gated")
                time.sleep(0.05)
                continue
            (root / "worker-gated").unlink(missing_ok=True)
            if worker.run_once() is None:
                time.sleep(0.1)
    finally:
        engine.dispose()


def source_fingerprint(history, repo, run):
    result = {}
    with history.engine.connect() as connection:
        for name in (
            "runs",
            "run_commits",
            "events",
            "artifacts",
            "external_calls",
            "sandbox_executions",
        ):
            table = TABLES[name]
            if name == "artifacts":
                links = TABLES["run_commits"]
                scope = table.c.commit_id.in_(
                    sa.select(links.c.commit_id).where(
                        links.c.repository_id == repo, links.c.run_id == run
                    )
                )
            else:
                field = table.c.id if name == "runs" else table.c.run_id
                scope = field == run
            data = [
                dict(row)
                for row in connection.execute(
                    sa.select(table).where(table.c.repository_id == repo, scope)
                ).mappings()
            ]
            result[name] = sorted(data, key=lambda item: json.dumps(public(item), sort_keys=True))
    raw = json.dumps(public(result), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def audit(manager, repo, run, root):
    history = manager.history
    source = manager.run(repo, run)
    assert source["status"] == "completed"
    artifacts = manager.artifacts(repo, run)["items"]
    assert len(artifacts) == 7
    statistics = next(
        item for item in artifacts if item["relative_path"] == "results/statistics.json"
    )
    value = json.loads(history.read_artifact(repo, UUID(statistics["id"])))
    assert value["claims"] == 9 and value["frameworks"] == 3 and value["sources"] == 6
    with history.engine.connect() as connection:
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.repository_id == repo
                )
            ).mappings()
        )
        executions = list(
            connection.execute(
                sa.select(TABLES["sandbox_executions"]).where(
                    TABLES["sandbox_executions"].c.repository_id == repo
                )
            ).mappings()
        )
    tools = sorted(
        {
            row["provider"]
            for row in calls
            if row["run_id"] == run and row["provider"].startswith("tool:")
        }
    )
    assert len(tools) == 5
    assert all(row["cleanup_confirmed"] for row in executions)
    names = {row["container_name"] for row in executions}
    remaining = []
    client = DockerEngine.local()
    for name in names:
        assert re.fullmatch("[a-z0-9-]+", name)
        try:
            client.request("GET", f"/containers/{name}/json")
            remaining.append(name)
        except EngineError as error:
            if error.status != 404:
                raise
    assert not remaining, "联调执行容器未实际移除"
    return {
        "source_fingerprint": source_fingerprint(history, repo, run),
        "source_branch": public(history.branch(repo)),
        "source_tools": tools,
        "source_usage": source["budget"]["usage"],
        "statistics": value,
        "executions": public([dict(row) for row in executions]),
        "remaining_execution_containers": remaining,
        "recorded_model_guard_hits": int((root / "model-guard-hits").exists()),
        "recorded_engine_guard_hits": int((root / "engine-guard-hits").exists()),
    }


def main():
    settings = Settings.from_env()
    settings.assert_test_isolation()
    token = os.environ["AGENTGIT_API_TOKEN"]
    schema = "test_desktop_" + uuid4().hex
    root = (ROOT / ".agentgit" / "desktop-e2e" / schema).resolve()
    assert root.parent == (ROOT / ".agentgit" / "desktop-e2e").resolve()
    root.mkdir(parents=True)
    admin = make_engine(settings, testing=True)
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = isolated_engine(schema)
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["testing"] = True
    worker = None
    server = None
    thread = None
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        history = HistoryService(engine, ObjectStore(root / "objects"))
        manager = ManagementService(
            ResearchService(history, root / "data"), actor=settings.operator_id
        )
        api_settings = settings.model_copy(update={"api_token": SecretStr(token)})
        app = create_app(api_settings, management=manager)
        traffic = []

        @app.middleware("http")
        async def fixture_availability(request, call_next):
            traffic.append(f"{request.method} {request.url.path}")
            if (root / "api-gate").exists():
                return JSONResponse(
                    {"error": {"code": "fixture_unavailable", "message": "测试 API 暂不可用"}},
                    status_code=503,
                )
            return await call_next(request)

        opened = socket.socket()
        opened.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[opened]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.05)
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", schema, str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 15
        while not (root / "worker-ready").exists():
            assert worker.poll() is None and time.monotonic() < deadline, "联调 Worker 启动失败"
            time.sleep(0.05)
        print(
            json.dumps(
                {
                    "ready": True,
                    "url": f"http://127.0.0.1:{opened.getsockname()[1]}",
                    "schema": schema,
                    "root": str(root),
                }
            ),
            flush=True,
        )
        for line in sys.stdin:
            request = json.loads(line)
            name = request["command"]
            if name == "stop":
                break
            if name == "guard":
                if request["enabled"]:
                    (root / "recorded-guard").write_text("guard")
                else:
                    (root / "recorded-guard").unlink(missing_ok=True)
                result = {"guard": request["enabled"]}
            elif name == "storage_audit":
                digest = hashlib.sha256()
                count = 0
                size = 0
                for path in sorted((root / "objects").rglob("*")):
                    if path.is_file():
                        data = path.read_bytes()
                        digest.update(path.relative_to(root / "objects").as_posix().encode())
                        digest.update(b"\0")
                        digest.update(hashlib.sha256(data).digest())
                        count += 1
                        size += len(data)
                with engine.connect() as connection:
                    migration = connection.execute(
                        sa.text("SELECT version_num FROM alembic_version")
                    ).scalar_one()
                result = {
                    "cas_digest": digest.hexdigest(),
                    "cas_files": count,
                    "cas_bytes": size,
                    "schema": migration,
                    "worker_alive": worker.poll() is None,
                    "source_fingerprint": source_fingerprint(
                        history, UUID(request["repository"]), UUID(request["source_run"])
                    ),
                }
            elif name == "traffic":
                result = {"posts": [item for item in traffic if item.startswith("POST ")]}
            elif name in {"worker_gate", "api_gate"}:
                assert isinstance(request["enabled"], bool)
                marker = root / name.replace("_", "-")
                if request["enabled"]:
                    marker.write_text("gated")
                    if name == "worker_gate":
                        deadline = time.monotonic() + 5
                        while not (root / "worker-gated").exists():
                            assert worker.poll() is None and time.monotonic() < deadline
                            time.sleep(0.05)
                else:
                    marker.unlink(missing_ok=True)
                result = {"gated": request["enabled"]}
            elif name == "provider_case":
                assert request["mode"] in {"normal", "missing_usage", "unknown"}
                (root / "provider-case").write_text(request["mode"])
                result = {"mode": request["mode"]}
            elif name == "provider_audit":
                entries = (
                    (root / "provider-calls").read_text().splitlines()
                    if (root / "provider-calls").exists()
                    else []
                )
                result = {
                    "transport": "deterministic_fixture",
                    "counts": entries.count("count"),
                    "generations": entries.count("generate"),
                    "model_guard_hits": int((root / "model-guard-hits").exists()),
                }
            elif name == "audit":
                result = audit(
                    manager,
                    UUID(request["repository"]),
                    UUID(request["source_run"]),
                    root,
                )
            else:
                raise AssertionError("未知联调控制命令")
            print(
                json.dumps({"id": request["id"], "result": result}, ensure_ascii=False), flush=True
            )
    finally:
        (root / "worker-stop").write_text("stop")
        if worker is not None:
            worker.wait(timeout=100)
            assert worker.returncode == 0, "联调 Worker 异常退出"
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        main()
