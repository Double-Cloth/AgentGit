"""真实 Repair Lab 联调：独立测试库/API、独立控制器与 Worker，仅使用固定 Mock。"""

import hashlib
import json
import os
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
from real_backend import ROOT, isolated_engine, source_fingerprint

sys.path.insert(0, str(ROOT / "tests" / "integration"))
from confirmation_evidence import capture_confirmation
from test_repair_confirmation import ConfirmationMock, adapter_binding
from test_repair_worker import create_case

from agentgit.api.main import create_app
from agentgit.config import Settings
from agentgit.core.research import ResearchGoal, evolve
from agentgit.repair.confirmation_worker import RepairConfirmationWorker
from agentgit.repair.study_worker import RepairStudyWorker
from agentgit.repair.worker import RepairSearchWorker
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.management import ManagementService
from agentgit.services.repair_confirmation import RepairConfirmationService
from agentgit.services.repair_jobs import RepairJobService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.research import ResearchService
from agentgit.storage.database import make_engine
from agentgit.storage.objects import ObjectStore
from agentgit.storage.schema import build_metadata
from agentgit.worker import Worker

TABLES = build_metadata().tables


def policy():
    return (
        "desktop-joint-fields/v1;harness-sha256="
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )


def history_for(schema, root):
    return HistoryService(isolated_engine(schema), ObjectStore(root / "objects"))


def worker_main(schema, root):
    history = history_for(schema, root)
    research = ResearchService(history, root / "seed" / "research")
    worker = Worker(JobService(research), model_factory=lambda: ConfirmationMock(policy()))
    controllers = [
        RepairSearchWorker(research),
        RepairStudyWorker(research),
        RepairConfirmationWorker(research),
    ]
    (root / "worker-ready").write_text("ready")
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists():
                (root / "worker-gated").write_text("gated")
                time.sleep(0.03)
                continue
            (root / "worker-gated").unlink(missing_ok=True)
            for controller in controllers:
                controller.run_once()
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def seed(history, root):
    repo, legacy, research, failed = create_case(
        history,
        root / "seed",
        mode="joint",
        adapter_binding=adapter_binding(policy()),
        budget_transform=lambda value: evolve(
            value, quotas=evolve(value.quotas, reduce=16), max_trials=40
        ),
    )
    original = RepairJobService(history).view(repo, legacy)
    RepairJobService(history).cancel(repo, legacy, actor="fixture")
    with history.engine.connect() as connection:
        source = history._run(connection, repo, failed.run_id)
    regression = []
    for index in range(4):
        task = research.create_task(
            repo,
            source["agent_config_id"],
            ResearchGoal(text=f"regression-{index};{adapter_binding(policy())};固定框架语义回归"),
        )
        run = research.start(repo, task)
        assert run.execute(ConfirmationMock(policy())).status.value == "completed"
        with history.engine.connect() as connection:
            links = TABLES["run_commits"]
            first = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        regression.append(dict(source_run_id=str(run.run_id), from_commit=first))
    binding = original["manifest"]["space"]["binding"]
    return dict(
        repository=str(repo),
        failed_run=str(failed.run_id),
        base=binding["base_commit"],
        validator=binding["validator_ref"],
        target_event=original["manifest"]["space"]["patches"][0]["intervention"]["target_event_id"],
        budget=original["manifest"]["budget"],
        regression=regression,
        source_fingerprint=source_fingerprint(history, repo, failed.run_id),
        policy=policy(),
    )


def main():
    settings = Settings.from_env()
    settings.assert_test_isolation()
    schema = "test_desktop_" + uuid4().hex
    root = (ROOT / ".agentgit" / "desktop-e2e" / schema).resolve()
    assert root.parent == (ROOT / ".agentgit" / "desktop-e2e").resolve()
    root.mkdir(parents=True)
    (root / "worker-gate").write_text("seed")
    admin = make_engine(settings, testing=True)
    with admin.begin() as connection:
        connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    history = history_for(schema, root)
    server = worker = thread = opened = None
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["testing"] = True
        with history.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        research = ResearchService(history, root / "seed" / "research")
        manager = ManagementService(research, actor=settings.operator_id)
        app = create_app(
            settings.model_copy(update={"api_token": SecretStr(os.environ["AGENTGIT_API_TOKEN"])}),
            management=manager,
        )
        mutations = []
        reduced_snapshots = {}

        @app.middleware("http")
        async def traffic(request, call_next):
            if request.method == "POST" and request.url.path.endswith("/open-validation"):
                study = UUID(request.url.path.split("/")[2])
                if str(study) not in reduced_snapshots:
                    with history.engine.connect() as connection:
                        row = (
                            connection.execute(
                                sa.select(TABLES["repair_studies"]).where(
                                    TABLES["repair_studies"].c.id == study
                                )
                            )
                            .mappings()
                            .one()
                        )
                    snapshot = RepairStudyService(history).view(row["repository_id"], study)
                    if snapshot["status"] == "awaiting_samples":
                        reduced_snapshots[str(study)] = snapshot
            response = await call_next(request)
            if request.method == "POST":
                mutations.append(
                    dict(
                        path=request.url.path,
                        key=request.headers.get("Idempotency-Key"),
                        status=response.status_code,
                    )
                )
            return response

        opened = socket.socket()
        opened.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[opened]), daemon=True)
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.03)
        worker = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", schema, str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        while not (root / "worker-ready").exists():
            assert worker.poll() is None and time.monotonic() < deadline
            time.sleep(0.03)
        print(
            json.dumps(
                dict(
                    ready=True,
                    url=f"http://127.0.0.1:{opened.getsockname()[1]}",
                    schema=schema,
                    root=str(root),
                )
            ),
            flush=True,
        )
        seeded = None
        for line in sys.stdin:
            request = json.loads(line)
            name = request["command"]
            if name == "stop":
                break
            if name == "seed":
                assert seeded is None
                seeded = seed(history, root)
                result = seeded
                (root / "worker-gate").unlink()
            elif name == "audit":
                assert seeded is not None and worker.poll() is None
                repo = UUID(seeded["repository"])
                search = UUID(request["search"])
                links = manager.repair_workflow(repo, search)
                source = manager.repair_search(repo, search)
                reduced = reduced_snapshots[links["study_id"]]
                current = RepairStudyService(history).view(repo, UUID(links["study_id"]))
                assert reduced["state_ref"] == current["state_ref"]
                assert reduced["final_lock"] == current["final_lock"]
                validation = RepairConfirmationService(research).view(repo, UUID(links["study_id"]))
                assert validation["status"] == "verified_under_fixture"
                assert (
                    source_fingerprint(history, repo, UUID(seeded["failed_run"]))
                    == seeded["source_fingerprint"]
                )
                os.environ["AGENTGIT_S10_NATIVE_REPAIR_EVIDENCE"] = str(
                    root / "repair-evidence.json"
                )
                bundle = capture_confirmation(
                    history,
                    validation,
                    source,
                    reduced,
                    case="S10-real-electron-joint-fields/v1",
                    environment="AGENTGIT_S10_NATIVE_REPAIR_EVIDENCE",
                )
                with history.engine.connect() as connection:
                    counts = {
                        name: connection.execute(
                            sa.select(sa.func.count()).select_from(TABLES[name])
                        ).scalar_one()
                        for name in ("jobs", "runs", "agent_configs", "proposal_reviews")
                    }
                result = dict(
                    links=links,
                    source_fingerprint=seeded["source_fingerprint"],
                    raw_audit=bundle["raw_audit"],
                    counts=counts,
                    evidence=str(root / "repair-evidence.json"),
                    mutations=mutations,
                )
            elif name == "state":
                assert seeded is not None
                with history.engine.connect() as connection:
                    counts = {
                        name: connection.execute(
                            sa.select(sa.func.count()).select_from(TABLES[name])
                        ).scalar_one()
                        for name in ("jobs", "runs", "agent_configs", "proposal_reviews")
                    }
                result = dict(
                    counts=counts,
                    source_fingerprint=source_fingerprint(
                        history, UUID(seeded["repository"]), UUID(seeded["failed_run"])
                    ),
                    mutations=mutations,
                )
            elif name == "mutations":
                result = dict(mutations=mutations)
            else:
                raise AssertionError("未知修复联调命令")
            print(json.dumps(dict(id=request["id"], result=result), ensure_ascii=False), flush=True)
    finally:
        (root / "worker-stop").write_text("stop")
        if worker is not None:
            worker.wait(timeout=120)
            assert worker.returncode == 0, "修复联调 Worker 异常结束"
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
        if opened is not None:
            opened.close()
        history.engine.dispose()
        with admin.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        main()
