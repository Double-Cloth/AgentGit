"""真实研究、显式 Live 分支、录制来源核对及只读完整候选验证。"""

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.merge import ConflictKind, MergeOrigin
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentConfig, AgentState, ResearchGoal, evolve
from agentgit.core.versions import ArtifactPatch, LiveOverrides, LiveReplay
from agentgit.runtime.validation import ResearchReport, render_report
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES
from agentgit.services.management import ManagementService, public
from agentgit.services.merge_validation import preview_research_merge
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def counts(history):
    with history.engine.connect() as connection:
        return {
            name: connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
            for name, table in TABLES.items()
        }


def cas(history):
    return {
        path.relative_to(history.objects.root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in history.objects.root.rglob("*")
        if path.is_file()
    }


def archive(history, repository, tmp_path, name, result, before, branches, statements=()):
    root = Path(os.environ.get("AGENTGIT_MERGE_EVIDENCE", str(tmp_path / "evidence")))
    folder = root / name
    folder.mkdir(parents=True, exist_ok=False)
    indexes = {}
    with history.engine.connect() as connection:
        for identity in (
            "commits",
            "commit_parents",
            "run_commits",
            "snapshots",
            "runs",
            "jobs",
            "events",
            "external_calls",
            "object_refs",
            "artifacts",
            "branches",
        ):
            table = TABLES[identity]
            indexes[identity] = public(
                [
                    dict(row)
                    for row in connection.execute(
                        sa.select(table).where(table.c.repository_id == repository)
                    ).mappings()
                ]
            )
        objects = TABLES["objects"]
        refs = TABLES["object_refs"]
        rows = list(
            connection.execute(
                sa.select(objects)
                .join(refs, refs.c.object_id == objects.c.id)
                .where(refs.c.repository_id == repository)
                .distinct()
            ).mappings()
        )
    assert len(rows) < 4000
    contents = {}
    total = 0
    for row in rows:
        payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
        total += len(payload)
        assert total <= 16 * 1024 * 1024
        contents[row["id"]] = dict(
            object_type=row["object_type"],
            byte_length=len(payload),
            base64=base64.b64encode(payload).decode("ascii"),
        )
    data = dict(
        kind="S11-research-preview-evidence/v1",
        source_revision=source_revision(),
        repository_id=str(repository),
        selection=result.selection.model_dump(mode="json") if result else None,
        state=result.state.proposed_state.model_dump(mode="json")
        if result and result.state.proposed_state
        else None,
        validation=result.validation.model_dump(mode="json") if result else None,
        artifacts=[
            dict(
                path=item.path,
                status=item.status,
                text=item.content.decode("utf-8") if item.content is not None else None,
                validation_pending=item.validation_pending,
            )
            for item in (result.artifacts if result else ())
        ],
        before_counts=before,
        branches=public(branches),
        indexes=indexes,
        objects=contents,
        raw_bytes=total,
        observed_sql=list(statements),
    )
    (folder / "evidence.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


@pytest.mark.parametrize("bad_artifact", [False, True], ids=["fresh-branches", "invalid-claim"])
def test_real_frozen_research_merge_is_read_only_and_rechecks_raw_artifacts(
    history, tmp_path, monkeypatch, bad_artifact
):
    revision = source_revision()
    repository = history.create_repository("S11 真正研究来源")
    research = ResearchService(history, tmp_path / "runs")
    cfg = evolve(AgentConfig(), limits=evolve(AgentConfig().limits, max_replans=0))
    config = research.create_config(repository, cfg)
    goal = ResearchGoal(text="比较三个 fixture 框架")
    validator = ValidatorService(history).freeze(repository, goal, Corpus.bundled())
    task = research.create_task(repository, config, goal)
    session = research.start(repository, task)
    state = session.execute()
    assert state.evaluation.passed
    with history.engine.connect() as connection:
        checkpoints = list(
            connection.execute(
                sa.select(TABLES["snapshots"].c.commit_id)
                .where(
                    TABLES["snapshots"].c.repository_id == repository,
                    TABLES["snapshots"].c.run_id == session.run_id,
                )
                .order_by(TABLES["snapshots"].c.event_seq)
            ).scalars()
        )
    base = (
        session.head
        if bad_artifact
        else next(
            commit
            for commit in checkpoints
            if (
                value := parse_contract(
                    encode(history.restore(repository, commit).data), AgentState
                )
            ).plan
            is not None
            and not value.completed
            and value.pending_tool is None
        )
    )
    manager = ManagementService(research)
    origins = []
    for branch in ("ours", "theirs"):
        overrides = LiveOverrides()
        if bad_artifact and branch == "ours":
            raw = next(
                item
                for item in history.restore(repository, base).artifacts
                if item.path == "sources.json"
            )
            report = parse_contract(
                history.objects.get(raw.object_ref, ObjectType.BLOB), ResearchReport
            )
            wrong = evolve(report.claims[0], quote="未出现在固定来源中的伪造引文")
            changed = evolve(report, claims=(wrong, *report.claims[1:]))
            overrides = LiveOverrides(
                artifacts=(
                    ArtifactPatch(
                        path="sources.json",
                        text=encode(changed.model_dump(mode="json")).decode(),
                        media_type="application/json",
                    ),
                    ArtifactPatch(
                        path="report.md", text=render_report(changed), media_type="text/markdown"
                    ),
                )
            )
        submitted = manager.replay(
            repository,
            LiveReplay(
                source_run_id=str(session.run_id),
                from_commit=base,
                branch_name=branch,
                overrides=overrides,
            ),
            branch,
        )
        finished = Worker(manager.jobs).run_once()
        assert finished["status"] == (
            "failed" if bad_artifact and branch == "ours" else "completed"
        )
        run = manager.jobs.run(repository, UUID(submitted["run_id"]))
        origins.append(
            MergeOrigin(run_id=submitted["run_id"], commit_id=run["checkpoint_commit_id"])
        )
    assert manager.jobs.claim("nothing-left") is None
    before = counts(history)
    objects = cas(history)
    original = {name: history.branch(repository, name) for name in ("main", "ours", "theirs")}
    case_name = "invalid-claim" if bad_artifact else "fresh-branches"
    archive(history, repository, tmp_path, case_name + "/source", None, before, original)
    statements = []

    def read_only_transaction(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    def select_only(_conn, _cursor, statement, _parameters, _context, _many):
        upper = statement.lstrip().upper()
        assert upper == "SET TRANSACTION READ ONLY" or (
            upper.startswith(("SELECT", "WITH RECURSIVE LINEAGE("))
            and not re.search(
                r"\b(INSERT|UPDATE|DELETE|MERGE|COPY|CALL|CREATE|DROP|ALTER|TRUNCATE|DO)\b", upper
            )
        )
        statements.append(statement)

    def reject(*_args, **_kwargs):
        pytest.fail("只读合并不能写 CAS 或调用模型/工具")

    monkeypatch.setattr(history.objects, "put", reject)
    monkeypatch.setattr("agentgit.adapters.mock.MockResearchModel.invoke", reject)
    monkeypatch.setattr("agentgit.adapters.tools.ToolRegistry.invoke", reject)
    sa.event.listen(history.engine, "before_cursor_execute", select_only)
    sa.event.listen(history.engine, "begin", read_only_transaction)
    try:
        result = preview_research_merge(history, repository, *origins, validator)
        again = preview_research_merge(history, repository, *origins, validator)
    finally:
        sa.event.remove(history.engine, "before_cursor_execute", select_only)
        sa.event.remove(history.engine, "begin", read_only_transaction)
    assert statements and result.validation == again.validation
    assert result.validation.validated is (not bad_artifact)
    assert result.validation.approval_required
    if bad_artifact:
        assert ConflictKind.FACT_CONTRADICTION in {
            item.kind for item in result.validation.conflicts
        }
        assert not result.validation.report_evaluation.passed
    else:
        assert result.validation.report_evaluation.passed
        assert len(result.state.proposed_state.read_proofs) == 12
        assert all(item.validation_pending == ("approval",) for item in result.artifacts)
    assert counts(history) == before and cas(history) == objects
    assert {name: history.branch(repository, name) for name in original} == original
    assert source_revision() == revision
    archive(
        history,
        repository,
        tmp_path,
        case_name + "/validated",
        result,
        before,
        original,
        statements,
    )


def test_foreign_validator_is_rejected_before_reading_sources(history, tmp_path):
    first = history.create_repository("验收器所属项目")
    other = history.create_repository("其他项目")
    validator = ValidatorService(history).freeze(
        first, ResearchGoal(text="比较三个框架"), Corpus.bundled()
    )
    before = counts(history)
    bogus = MergeOrigin(run_id="00000000-0000-0000-0000-000000000001", commit_id="a" * 64)
    with pytest.raises(AgentGitError) as error:
        preview_research_merge(history, other, bogus, bogus, validator)
    assert error.value.code == ErrorCode.NOT_FOUND and counts(history) == before
