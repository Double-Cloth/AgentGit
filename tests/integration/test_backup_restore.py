"""在自建空测试库恢复 PostgreSQL/CAS；不停止或修改业务服务。"""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from test_repair_worker import create_case, drive

from agentgit.adapters.mock import MockResearchModel
from agentgit.config import Settings, database_url
from agentgit.core.canonical import ObjectType
from agentgit.core.repair_studies import NewRepairStudy
from agentgit.core.research import ResearchGoal
from agentgit.services.history import TABLES, HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def fingerprints(engine):
    """逐表保存与行顺序无关的内容摘要，包含迁移及全部外键索引。"""
    metadata = sa.MetaData()
    metadata.reflect(bind=engine)
    result = {}
    with engine.connect() as connection:
        for name, table in sorted(metadata.tables.items()):
            rows = sorted(
                json.dumps(dict(row), sort_keys=True, default=str, ensure_ascii=True)
                for row in connection.execute(sa.select(table)).mappings()
            )
            result[name] = dict(
                rows=len(rows), sha256=hashlib.sha256("\n".join(rows).encode()).hexdigest()
            )
    return result


def docker_pg(container, command, *, data=None):
    result = subprocess.run(
        ["docker", "exec", "-i", container, *command],
        input=data,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def enqueue(research, name):
    repository = research.history.create_repository(name)
    config = research.create_config(repository)
    task = research.create_task(repository, config, ResearchGoal(text="比较三个 fixture 框架"))
    queue = JobService(research)
    submission = queue.enqueue(
        repository,
        task,
        actor="backup-fixture",
        key="first",
        config_version=1,
        expected_branch_version=0,
        expected_head=None,
    )
    return queue, repository, submission


class ProcessInterrupted(BaseException):
    """绕过正常 Adapter 错误结算，保留已写入但响应未落库的调用。"""


class InterruptAfterWrite(Worker):
    def _attach(self, lease):
        session, origins, remaining = super()._attach(lease)
        original = session.tools.invoke

        def invoke(name, *args, **kwargs):
            response = original(name, *args, **kwargs)
            if name == "artifact_write":
                raise ProcessInterrupted()
            return response

        session.tools.invoke = invoke
        return session, origins, remaining


class NoNewCalls(MockResearchModel):
    def invoke(self, payload):
        raise AssertionError("备份恢复后的未决动作不得自动重发模型调用")


class RecoveryWithoutTools(Worker):
    def _attach(self, lease):
        session, origins, remaining = super()._attach(lease)

        def forbidden(*args, **kwargs):
            raise AssertionError("备份恢复后的未决写动作不得自动重发工具调用")

        session.tools.invoke = forbidden
        return session, origins, remaining


@pytest.mark.parametrize("milestone", ["D1", "D2"])
def test_database_and_cas_restore_into_empty_database(history, tmp_path, milestone):
    settings = Settings.from_env()
    settings.assert_test_isolation()
    url = database_url(settings.test_database_url)
    assert url.host == "127.0.0.1" and url.port == 55432
    assert url.database == "agentgit_test" and url.username == "agentgit"
    container = "agentgit-postgres-1"
    assert shutil.which("docker"), "此验收需要既有本机 PostgreSQL 容器的 pg_dump/pg_restore"
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
    assert re.fullmatch(r"test_[0-9a-f]{32}", schema)
    # 确认容器内客户端访问的是当前随机隔离 schema，而非另一个集群。
    found = docker_pg(
        container,
        [
            "psql",
            "-U",
            url.username,
            "-d",
            url.database,
            "-Atc",
            f"SELECT nspname FROM pg_namespace WHERE nspname='{schema}'",
        ],
    )
    assert found.decode().strip() == schema

    research = ResearchService(history, tmp_path / "source-research")
    completed_queue, completed_repo, completed = enqueue(research, "备份完整产物")
    assert Worker(completed_queue).run_once()["status"] == "completed"
    study = study_repo = None
    if milestone == "D2":
        study_repo, search, repair_research, _ = create_case(history, tmp_path / "repair")
        assert drive(repair_research, study_repo, search)["status"] == "needs_confirmation"
        study = UUID(
            RepairStudyService(history).submit(
                study_repo,
                NewRepairStudy(search_id=str(search)),
                actor="backup-fixture",
                key="study",
            )["study_id"]
        )
    queue, repository, pending = enqueue(research, "备份中断写调用")
    with pytest.raises(ProcessInterrupted):
        InterruptAfterWrite(queue).run_once()
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["jobs"]
            .update()
            .where(TABLES["jobs"].c.id == UUID(pending.job_id))
            .values(lease_until=sa.func.now() - sa.text("interval '1 second'"))
        )
        calls = list(
            connection.execute(
                sa.select(TABLES["external_calls"]).where(
                    TABLES["external_calls"].c.run_id == UUID(pending.run_id),
                    TABLES["external_calls"].c.result_ref.is_(None),
                )
            ).mappings()
        )
    assert len(calls) == 1 and calls[0]["provider"] == "tool:artifact_write/v1"
    # 此处所有本测试写者已返回，心跳线程已 join；不会停止共享数据库或外部 Worker。
    before = fingerprints(history.engine)
    source_branch = history.branch(completed_repo)
    pending_before = queue.get(repository, UUID(pending.job_id))
    evidence = ROOT / ".agentgit/evidence/S10-backup-restore-v1" / (milestone + "-" + uuid4().hex)
    evidence.mkdir(parents=True, exist_ok=False)
    dump = docker_pg(
        container,
        [
            "pg_dump",
            "-U",
            url.username,
            "-d",
            url.database,
            "--schema",
            schema,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
        ],
    )
    (evidence / "database.dump").write_bytes(dump)
    backup_cas = evidence / "cas"
    shutil.copytree(history.objects.root, backup_cas)
    assert fingerprints(history.engine) == before

    target_name = "backup_" + uuid4().hex + "_test"
    assert re.fullmatch(r"backup_[0-9a-f]{32}_test", target_name)
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    restored_engine = None
    created = False
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{target_name}" TEMPLATE template0'))
        created = True
        restored_engine = create_engine(
            url.set(database=target_name),
            hide_parameters=True,
            connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        )
        with restored_engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema=:schema"
                    ),
                    {"schema": schema},
                ).scalar_one()
                == 0
            )
        docker_pg(
            container,
            [
                "pg_restore",
                "-U",
                url.username,
                "-d",
                target_name,
                "--single-transaction",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
            ],
            data=dump,
        )
        restored_cas = tmp_path / "restored-cas"
        shutil.copytree(backup_cas, restored_cas)
        restored = HistoryService(restored_engine, ObjectStore(restored_cas))
        assert fingerprints(restored_engine) == before
        assert restored.branch(completed_repo) == source_branch
        restored_queue = JobService(ResearchService(restored, tmp_path / "restored-research"))
        assert restored_queue.get(repository, UUID(pending.job_id)) == pending_before
        with restored_engine.connect() as connection:
            objects = list(connection.execute(sa.select(TABLES["objects"])).mappings())
            commits = list(connection.execute(sa.select(TABLES["commits"])).mappings())
            references = connection.execute(
                sa.select(sa.func.count()).select_from(TABLES["object_refs"])
            ).scalar_one()
        for item in objects:
            assert (
                len(restored.objects.get(item["id"], ObjectType(item["object_type"])))
                == item["byte_length"]
            )
        for item in commits:
            assert restored.restore(item["repository_id"], item["id"]) == history.restore(
                item["repository_id"], item["id"]
            )
        assert restored_queue.run(completed_repo, UUID(completed.run_id))["status"] == "completed"
        old_call_count = before["external_calls"]["rows"]
        recovered = RecoveryWithoutTools(restored_queue, model_factory=NoNewCalls).run_once()
        assert recovered["status"] == "outcome_unknown"
        assert RecoveryWithoutTools(restored_queue, model_factory=NoNewCalls).run_once() is None
        pending_after = restored_queue.get(repository, UUID(pending.job_id))
        assert pending_after["status"] == "outcome_unknown"
        assert pending_after["checkpoint_commit_id"]
        assert fingerprints(restored_engine)["external_calls"]["rows"] == old_call_count
        if study:
            assert RepairStudyService(restored).view(study_repo, study)["status"] == "queued"
        assert fingerprints(history.engine) == before
        summary = dict(
            kind="database-cas-restore-acceptance/v1",
            milestone=milestone,
            scope="current-schema isolated fixture; no production backup or historical D1 claim",
            source_schema=schema,
            target_database=target_name,
            empty_target=True,
            writer_stopped=True,
            tables=before,
            objects=len(objects),
            references=references,
            commits=len(commits),
            database_dump_sha256=hashlib.sha256(dump).hexdigest(),
            recovered_status=pending_after["status"],
            new_external_calls=0,
            source_unchanged=True,
            repair_study_preserved=bool(study),
        )
        with (evidence / "summary.json").open("x", encoding="utf-8") as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        if created:
            # 只删除当前函数成功创建、名称已校验的空恢复测试库；不强制断开其他连接。
            with admin.connect() as connection:
                connection.execute(sa.text(f'DROP DATABASE "{target_name}"'))
        admin.dispose()
