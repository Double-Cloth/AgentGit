"""四角色桌面联调：独立 Schema、真实 API/Worker 与可核对的响应丢失。"""

import base64
import json
import sys
import time
from pathlib import Path
from uuid import UUID

import repair_backend as base
import sqlalchemy as sa
from real_backend import source_fingerprint
from starlette.responses import JSONResponse

from agentgit.core.canonical import ObjectType
from agentgit.services.environment import dependency_lock_digest, source_revision
from agentgit.services.management import public

original_app = base.create_app


def create_app(*args, **kwargs):
    app = original_app(*args, **kwargs)
    root = kwargs["management"].history.objects.root.parent

    @app.middleware("http")
    async def unreliable_reply(request, call_next):
        if (root / "offline").exists():
            return JSONResponse(
                {"error": {"code": "fixture_offline", "message": "测试连接暂不可用"}},
                status_code=503,
            )
        response = await call_next(request)
        drop = root / "lost-reply"
        if (
            request.method == "POST"
            and drop.exists()
            and request.url.path.endswith(drop.read_text("utf-8"))
        ):
            drop.unlink()
            return JSONResponse(
                {"error": {"code": "fixture_reply_lost", "message": "提交已处理，响应未确认"}},
                status_code=503,
            )
        return response

    return app


base.create_app = create_app


def worker_main(schema, root):
    history = base.history_for(schema, root)
    queue = base.JobService(base.ResearchService(history, root / "seed" / "research"))
    worker = base.Worker(queue)
    (root / "worker-ready").write_text("ready")
    guarded = False
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists() or (root / "collaboration-hold").exists():
                time.sleep(0.03)
                continue
            if (root / "recorded-only").exists() and not guarded:
                from agentgit.adapters.collaboration_model import MockCollaborationModel
                from agentgit.adapters.tools import ToolRegistry
                from agentgit.adapters.workspace import ArtifactWorkspace

                def forbidden(*args, **kwargs):
                    raise AssertionError("Recorded 不得构造真实模型、工具或工作区")

                MockCollaborationModel.__init__ = forbidden
                ToolRegistry.__init__ = forbidden
                ArtifactWorkspace.__init__ = forbidden
                guarded = True
                (root / "recorded-guard-ready").write_text("ready")
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def seed(history, root):
    (root / "collaboration-frozen.json").write_text(
        json.dumps(dict(source=source_revision(), lock=dependency_lock_digest())), encoding="utf-8"
    )
    (root / "collaboration-hold").write_text("hold")
    return dict(repository=str(history.create_repository("四角色桌面真实研究")))


def extra_command(name, request, history, root, seeded, worker):
    assert worker.poll() is None
    assert json.loads((root / "collaboration-frozen.json").read_text("utf-8")) == dict(
        source=source_revision(), lock=dependency_lock_digest()
    )
    if name == "hold":
        if request["enabled"]:
            (root / "collaboration-hold").write_text("hold")
        else:
            (root / "collaboration-hold").unlink(missing_ok=True)
        return dict(held=request["enabled"])
    if name == "lost_reply":
        assert request["suffix"] in ("/prepare", "/start", "/control", "/recorded")
        (root / "lost-reply").write_text(request["suffix"], encoding="utf-8")
        return dict(armed=True)
    if name == "offline":
        if request["enabled"]:
            (root / "offline").write_text("offline")
        else:
            (root / "offline").unlink(missing_ok=True)
        return dict(offline=request["enabled"])
    if name == "recorded_guard":
        (root / "recorded-only").write_text("guard")
        deadline = time.monotonic() + 10
        while not (root / "recorded-guard-ready").exists():
            assert worker.poll() is None and time.monotonic() < deadline
            time.sleep(0.03)
        return dict(guarded=True)
    assert name == "inspect"
    with history.engine.connect() as c:
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
    packet = public(
        dict(
            kind="collaboration-desktop-raw/v1",
            source_revision=source_revision(),
            lock_digest=dependency_lock_digest(),
            schema_head=head,
            tables=tables,
            objects=objects,
            fingerprints=fingerprints,
        )
    )
    target = root / f"collaboration-inspect-{request['id']}.json"
    target.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(
        evidence=str(target),
        counts={name: len(rows) for name, rows in tables.items()},
        fingerprints=fingerprints,
        groups=public(tables["collaborations"]),
        calls=public(tables["external_calls"]),
    )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        base.main(seed_case=seed, extra_command=extra_command)
