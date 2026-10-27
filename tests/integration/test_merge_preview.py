"""真实 PostgreSQL/CAS 上的产物候选，证明只读且不提前发布合并历史。"""

import hashlib
import json
from dataclasses import asdict

import pytest
import sqlalchemy as sa

from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EnvironmentManifest
from agentgit.core.versions import NewBranch
from agentgit.services.history import TABLES, ArtifactInput, EventInput
from agentgit.services.merge import MergePreviewService
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


@pytest.fixture
def branches(history):
    repository = history.create_repository("S11 只读产物候选")
    run = history.create_run(repository)
    environment = EnvironmentManifest(
        runtime_version="fixture",
        python_version="3.12",
        code_revision="fixture/s11-preview",
        dependency_lock_digest="a" * 64,
        model_signature="fixture",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repository, b"fixture"),
        evaluation_time="2026-10-20T00:00:00Z",
        random_seed=0,
    )
    text = "# 报告\n## A\na\n## B\nb\n"

    def publish(run_id, head, version, content):
        return history.commit(
            repository,
            run_id,
            expected_head=head,
            expected_version=version,
            state={"fixture": "artifact-preview", "executable": False},
            environment=environment,
            events=(EventInput("fixture.artifact", "write-report", {}),),
            artifacts=(ArtifactInput("report.md", content.encode(), "text/markdown"),),
        ).commit_id

    base = publish(run, None, 0, text)
    forks = VersionService(history)
    for name in ("ours", "theirs"):
        forks.fork(
            repository,
            NewBranch(source_run_id=str(run), from_commit=base, name=name, expected_version=0),
            actor="fixture",
        )
    return repository, base, text, publish


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for name, table in TABLES.items()
        }


def cas_bytes(history):
    return {
        path.relative_to(history.objects.root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in history.objects.root.rglob("*")
        if path.is_file()
    }


def archive(history, root, name, selection, candidate, before, statements):
    # 仅归档测试新建数据；完整原始对象便于后续从输入字节重算候选。
    folder = root / name
    folder.mkdir(parents=True, exist_ok=False)
    value = {
        "selection": selection.model_dump(mode="json"),
        "candidate": asdict(candidate),
        "database_counts": before,
        "sql_read_only": all(item.lstrip().upper().startswith("SELECT") for item in statements),
        "objects_sha256": cas_bytes(history),
    }
    value["candidate"]["content"] = (
        candidate.content.decode("utf-8") if candidate.content is not None else None
    )
    value["candidate"]["conflicts"] = [item.model_dump(mode="json") for item in candidate.conflicts]
    for relative in value["objects_sha256"]:
        destination = folder / "objects" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((history.objects.root / relative).read_bytes())
    (folder / "result.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


@pytest.mark.parametrize("overlap", [False, True], ids=["independent", "conflict"])
def test_real_artifact_preview_is_read_only_and_keeps_every_head(
    history, branches, monkeypatch, tmp_path, overlap
):
    repository, base, text, publish = branches
    heads = []
    for name, content in (
        ("ours", text.replace("a\n", "left\n")),
        ("theirs", text.replace("a\n" if overlap else "b\n", "right\n")),
    ):
        heads.append(publish(history.create_run(repository, name), base, 0, content))
    before = counts(history)
    objects = cas_bytes(history)
    original = {name: history.branch(repository, name) for name in ("main", "ours", "theirs")}
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
        assert statement.lstrip().upper().startswith("SELECT")

    def reject_write(*_args, **_kwargs):
        pytest.fail("候选预览不能发布 CAS 或 Commit")

    monkeypatch.setattr(history.objects, "put", reject_write)
    sa.event.listen(history.engine, "before_cursor_execute", observe)
    try:
        selected, candidate = MergePreviewService(history).artifact(repository, *heads, "report.md")
    finally:
        sa.event.remove(history.engine, "before_cursor_execute", observe)
    assert selected.selected == base
    assert candidate.status == ("conflict" if overlap else "merged")
    assert candidate.content == (
        None if overlap else text.replace("a\n", "left\n").replace("b\n", "right\n").encode()
    )
    assert "approval" in candidate.validation_pending
    assert counts(history) == before and cas_bytes(history) == objects
    assert {name: history.branch(repository, name) for name in original} == original
    archive(
        history,
        tmp_path / "preview-evidence",
        "conflict" if overlap else "independent",
        selected,
        candidate,
        before,
        statements,
    )


def test_merge_base_reader_rejects_cross_repository_commits(history, branches):
    repository, base, _, _ = branches
    other = history.create_repository("无权项目")
    before = counts(history)
    with pytest.raises(AgentGitError) as error:
        MergePreviewService(history).bases(other, base, base)
    assert error.value.code == ErrorCode.NOT_FOUND
    assert counts(history) == before
