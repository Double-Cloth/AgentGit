"""A2A桌面联调：独立协议进程／Schema／API／Worker，原始包不导出凭据。"""

import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import UUID

import collaboration_backend as harness
import sqlalchemy as sa
from merge_backend import frozen_files
from real_backend import source_fingerprint
from starlette.responses import JSONResponse

from agentgit.core.canonical import ObjectType
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.management import public

base = harness.base
PROTOCOL = None
PROTOCOL_ROOT = None
CASE_ROOT = None
original_app = harness.original_app


def traced_app(*args, **kwargs):
    app = original_app(*args, **kwargs)
    root = kwargs["management"].history.objects.root.parent

    @app.middleware("http")
    async def trace_response(request, call_next):
        frozen_run = root / "a2a-frozen-run.json"
        if request.method == "GET" and frozen_run.exists():
            cached = json.loads(frozen_run.read_text("utf-8"))
            if request.url.path == f"/runs/{cached['id']}":
                return JSONResponse(cached)
        response = await call_next(request)
        if request.method == "POST":
            with (root / "a2a-original-responses.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        dict(
                            path=request.url.path,
                            status=response.status_code,
                            key=request.headers.get("Idempotency-Key"),
                        )
                    )
                    + "\n"
                )
        return response

    return app


harness.original_app = traced_app


def wait_file(path, process, seconds=15):
    until = time.monotonic() + seconds
    while True:
        assert process.poll() is None, "实际进程提前退出"
        assert time.monotonic() < until, "实际进程未按期就绪"
        try:
            return path.read_text("utf-8")
        except (FileNotFoundError, PermissionError):
            time.sleep(0.02)


def worker_main(schema, root):
    history = base.history_for(schema, root)
    worker = base.Worker(base.JobService(base.ResearchService(history, root / "seed/research")))
    (root / "worker-ready").write_text(str(os.getpid()))
    guarded = False
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists() or (root / "a2a-hold").exists():
                (root / "a2a-held").write_text("held")
                time.sleep(0.03)
                continue
            (root / "a2a-held").unlink(missing_ok=True)
            if (root / "recorded-only").exists() and not guarded:
                from agentgit.adapters.a2a import A2AClient

                def forbidden(*args, **kwargs):
                    raise AssertionError("Recorded不得构造协议客户端")

                A2AClient.__init__ = forbidden
                for name in tuple(os.environ):
                    if name.startswith("AGENTGIT_A2A_") and name.endswith("_TOKEN"):
                        del os.environ[name]
                guarded = True
                (root / "recorded-guard-ready").write_text(str(os.getpid()))
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def seed(history, root):
    global CASE_ROOT

    CASE_ROOT = root
    assert PROTOCOL_ROOT is not None and PROTOCOL is not None
    manifest = frozen_files()
    for name in (
        "tests/helpers/a2a_server.py",
        "tests/fixtures/a2a/v1/agent-card.json",
        "tests/fixtures/a2a/v1/cases.json",
    ):
        import hashlib

        manifest[name] = hashlib.sha256((base.ROOT / name).read_bytes()).hexdigest()
    (root / "a2a-frozen.json").write_text(
        json.dumps(dict(source=source_revision(), lock=dependency_lock_digest(), files=manifest)),
        encoding="utf-8",
    )
    ready = json.loads(wait_file(PROTOCOL_ROOT / "ready.json", PROTOCOL))
    assert ready["pid"] == PROTOCOL.pid != os.getpid()
    return dict(
        repository=str(history.create_repository("A2A桌面公开资料")),
        endpoint=f"http://127.0.0.1:{ready['port']}",
        server="local-research",
        principal="operator",
    )


def extra_command(name, request, history, root, seeded, worker):
    assert worker.poll() is None and PROTOCOL.poll() is None
    frozen = json.loads((root / "a2a-frozen.json").read_text("utf-8"))
    assert frozen["source"] == source_revision() and frozen["lock"] == dependency_lock_digest()
    import hashlib

    for path, expected in frozen["files"].items():
        assert hashlib.sha256((base.ROOT / path).read_bytes()).hexdigest() == expected, path
    if name == "lost_reply":
        assert request["suffix"] in (
            "/a2a-runs",
            "/pause",
            "/resume",
            "/cancel",
            "/decision",
            "/branches",
        )
        (root / "lost-reply").write_text(request["suffix"], encoding="utf-8")
        return dict(armed=True)
    if name == "bump_branch":
        # 仅隔离Schema模拟另一操作者推进分支版本，不创建任务或协议请求。
        branches = base.TABLES["branches"]
        with history.engine.begin() as c:
            version = c.execute(
                branches.update()
                .where(
                    branches.c.repository_id == UUID(seeded["repository"]),
                    branches.c.name == "main",
                    branches.c.active_writer_run_id.is_(None),
                )
                .values(version=branches.c.version + 1)
                .returning(branches.c.version)
            ).scalar_one()
        return dict(version=version)
    if name in {"bump_version", "version"}:
        # 仅隔离Schema注入乐观版本竞争，状态／审批／调用不改变。
        runs = base.TABLES["runs"]
        if name == "bump_version":
            manager = base.ManagementService(
                base.ResearchService(history, root / "seed/research"),
                actor=base.Settings.from_env().operator_id,
            )
            (root / "a2a-frozen-run.json").write_text(
                json.dumps(public(manager.run(UUID(seeded["repository"]), UUID(request["run"])))),
                encoding="utf-8",
            )
        with history.engine.begin() as c:
            where = (runs.c.repository_id == UUID(seeded["repository"])) & (
                runs.c.id == UUID(request["run"])
            )
            if name == "bump_version":
                version = c.execute(
                    runs.update()
                    .where(where)
                    .values(state_version=runs.c.state_version + 1)
                    .returning(runs.c.state_version)
                ).scalar_one()
            else:
                version = c.execute(sa.select(runs.c.state_version).where(where)).scalar_one()
        return dict(version=version)
    if name == "release_run_reads":
        (root / "a2a-frozen-run.json").unlink(missing_ok=True)
        return dict(released=True)
    if name == "offline":
        if request["enabled"]:
            (root / "offline").write_text("offline")
        else:
            (root / "offline").unlink(missing_ok=True)
        return dict(offline=request["enabled"])
    if name == "mode":
        assert request["value"] in (
            "success",
            "working",
            "disconnect_after_accept",
            "input_required",
            "auth_required",
        )
        (PROTOCOL_ROOT / "mode.json").write_text(
            json.dumps(dict(mode=request["value"])), encoding="utf-8"
        )
        return dict(mode=request["value"])
    if name == "hold":
        if request["enabled"]:
            (root / "a2a-hold").write_text("hold")
            wait_file(root / "a2a-held", worker)
        else:
            (root / "a2a-hold").unlink(missing_ok=True)
        return dict(held=request["enabled"])
    if name == "recorded_guard":
        (root / "recorded-only").write_text("guard")
        assert (
            int(wait_file(root / "recorded-guard-ready", worker))
            == int((root / "worker-ready").read_text())
            != os.getpid()
        )
        return dict(guarded=True)
    assert name == "inspect"
    with history.engine.connect().execution_options(isolation_level="REPEATABLE READ") as c:
        tables = {
            name: [dict(row) for row in c.execute(sa.select(table)).mappings()]
            for name, table in base.TABLES.items()
        }
        head = c.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    objects = [
        dict(
            id=row["id"],
            type=row["object_type"],
            base64=base64.b64encode(
                history.objects.get(row["id"], ObjectType(row["object_type"]))
            ).decode("ascii"),
        )
        for row in tables["objects"]
    ]
    fingerprints = {
        str(row["id"]): source_fingerprint(history, UUID(seeded["repository"]), row["id"])
        for row in tables["runs"]
    }
    journal = PROTOCOL_ROOT / "state.json"
    protocol = (
        json.loads(wait_file(journal, PROTOCOL))
        if journal.exists()
        else dict(requests=[], accepted_messages=0, tasks={}, messages={})
    )
    packet = public(
        dict(
            kind="a2a-desktop-raw/v1",
            source_revision=source_revision(),
            lock_digest=dependency_lock_digest(),
            schema_head=head,
            tables=tables,
            objects=objects,
            fingerprints=fingerprints,
            protocol=protocol,
            original_responses=[
                json.loads(line)
                for line in (root / "a2a-original-responses.jsonl").read_text("utf-8").splitlines()
            ],
            processes=dict(
                api=os.getpid(),
                worker=int((root / "worker-ready").read_text()),
                protocol=PROTOCOL.pid,
            ),
        )
    )
    target = root / f"a2a-inspect-{request['id']}.json"
    target.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(
        evidence=str(target),
        counts={name: len(rows) for name, rows in tables.items()},
        fingerprints=fingerprints,
        protocol=protocol,
        calls=public(tables["external_calls"]),
    )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        with tempfile.TemporaryDirectory(prefix="agentgit-a2a-desktop-") as directory:
            PROTOCOL_ROOT = Path(directory)
            (PROTOCOL_ROOT / "mode.json").write_text('{"mode":"success"}', encoding="utf-8")
            environment = dict(os.environ)
            for role in ("owner", "other", "expired"):
                environment[f"AGENTGIT_A2A_FIXTURE_{role.upper()}_TOKEN"] = secrets.token_hex(24)
            os.environ["AGENTGIT_A2A_LOCAL_RESEARCH_OPERATOR_TOKEN"] = environment[
                "AGENTGIT_A2A_FIXTURE_OWNER_TOKEN"
            ]
            with (PROTOCOL_ROOT / "process.log").open("wb") as log:
                PROTOCOL = subprocess.Popen(
                    [
                        str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()),
                        str(base.ROOT / "tests/helpers/a2a_server.py"),
                        str(PROTOCOL_ROOT / "ready.json"),
                        str(PROTOCOL_ROOT / "state.json"),
                        str(PROTOCOL_ROOT / "mode.json"),
                    ],
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                try:
                    base.main(seed_case=seed, extra_command=extra_command)
                finally:
                    PROTOCOL.terminate()
                    PROTOCOL.wait(timeout=10)
                    if CASE_ROOT is not None:
                        (CASE_ROOT / "a2a-protocol-terminal.json").write_text(
                            json.dumps(
                                dict(
                                    pid=PROTOCOL.pid,
                                    exit_code=PROTOCOL.returncode,
                                    state=json.loads(
                                        (PROTOCOL_ROOT / "state.json").read_text("utf-8")
                                    )
                                    if (PROTOCOL_ROOT / "state.json").exists()
                                    else None,
                                )
                            ),
                            encoding="utf-8",
                        )
