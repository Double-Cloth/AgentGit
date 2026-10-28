"""耐久合并的真实来源和原始归档；每例独立 Schema，不读研究答案。"""

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.merge_proposals import MergeBranchPin, NewMergeProposal
from agentgit.core.models import CommitManifest, EnvironmentManifest, parse_contract
from agentgit.core.research import AgentConfig, ResearchGoal, evolve
from agentgit.core.versions import ArtifactPatch, LiveOverrides, LiveReplay
from agentgit.runtime.validation import ResearchReport, render_report
from agentgit.services.environment import source_revision
from agentgit.services.history import TABLES, ArtifactInput, EventInput
from agentgit.services.management import ManagementService, public
from agentgit.services.research import ResearchService
from agentgit.services.validators import ValidatorService
from agentgit.worker import Worker


def rows_for(history, repository):
    rows = {}
    with history.engine.connect() as connection:
        for name, table in TABLES.items():
            if "repository_id" in table.c:
                found = list(
                    connection.execute(
                        sa.select(table).where(table.c.repository_id == repository).limit(20001)
                    ).mappings()
                )
                assert len(found) <= 20000
                rows[name] = sorted((public(dict(row)) for row in found), key=encode)
    return rows


def history_fingerprint(history, repository):
    rows = rows_for(history, repository)
    return {
        name: hashlib.sha256(encode(values)).hexdigest()
        for name, values in rows.items()
        if not name.startswith("merge_") and name != "object_refs"
    }


def archive(history, repository, tmp_path, name, details):
    root = Path(os.environ.get("AGENTGIT_MERGE_PROPOSAL_EVIDENCE", str(tmp_path / "evidence")))
    folder = root / name
    folder.mkdir(parents=True, exist_ok=False)
    rows = rows_for(history, repository)
    with history.engine.connect() as connection:
        objects, refs = TABLES["objects"], TABLES["object_refs"]
        owned = list(
            connection.execute(
                sa.select(objects)
                .join(refs, refs.c.object_id == objects.c.id)
                .where(refs.c.repository_id == repository)
                .distinct()
            ).mappings()
        )
    assert len(owned) < 4000
    contents = {}
    total = 0
    for row in owned:
        payload = history.objects.get(row["id"], ObjectType(row["object_type"]))
        total += len(payload)
        assert total <= 32 * 1024 * 1024
        contents[row["id"]] = dict(
            object_type=row["object_type"],
            byte_length=len(payload),
            base64=base64.b64encode(payload).decode("ascii"),
        )
    value = dict(
        kind="S11-merge-proposal-evidence/v1",
        repository_id=str(repository),
        source_revision=source_revision(),
        indexes=rows,
        objects=contents,
        raw_bytes=total,
        details=public(details),
    )
    (folder / "evidence.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


@pytest.fixture
def merge_case(history, tmp_path, request):
    revision = source_revision()
    bad = getattr(request, "param", False)
    repo = history.create_repository("耐久合并真实来源")
    research = ResearchService(history, tmp_path / "runs")
    cfg = evolve(AgentConfig(), limits=evolve(AgentConfig().limits, max_replans=0))
    config = research.create_config(repo, cfg)
    goal = ResearchGoal(text="比较三个 fixture 框架")
    validator = ValidatorService(history).freeze(repo, goal, Corpus.bundled())
    task = research.create_task(repo, config, goal)
    session = research.start(repo, task)
    assert session.execute().evaluation.passed
    manager = ManagementService(research)
    pins = []
    for side in ("ours", "theirs"):
        # 不同环境给出真正不同的 HEAD；报告仍来自同一冻结语料。
        overrides = LiveOverrides(
            config=evolve(cfg, prompt=cfg.prompt + "\nfixture branch " + side)
        )
        if bad and side == "ours":
            entry = next(
                item
                for item in history.restore(repo, session.head).artifacts
                if item.path == "sources.json"
            )
            report = parse_contract(
                history.objects.get(entry.object_ref, ObjectType.BLOB), ResearchReport
            )
            changed = evolve(
                report,
                claims=(evolve(report.claims[0], quote="不在原文中的引文"), *report.claims[1:]),
            )
            overrides = evolve(
                overrides,
                artifacts=(
                    ArtifactPatch(
                        path="sources.json",
                        text=encode(changed.model_dump(mode="json")).decode(),
                        media_type="application/json",
                    ),
                    ArtifactPatch(
                        path="report.md", text=render_report(changed), media_type="text/markdown"
                    ),
                ),
            )
        submitted = manager.replay(
            repo,
            LiveReplay(
                source_run_id=str(session.run_id),
                from_commit=session.head,
                branch_name=side,
                overrides=overrides,
            ),
            side,
        )
        assert Worker(manager.jobs).run_once()["status"] == (
            "failed" if bad and side == "ours" else "completed"
        )
        branch = history.branch(repo, side)
        pins.append(
            MergeBranchPin(
                name=side,
                source_run_id=submitted["run_id"],
                head=branch["head_commit_id"],
                version=branch["version"],
            )
        )
    proposal = NewMergeProposal(ours=pins[0], theirs=pins[1], validator_ref=validator)
    assert source_revision() == revision
    archive(
        history,
        repo,
        tmp_path,
        request.node.name + "/source",
        dict(request=proposal.model_dump(mode="json")),
    )
    return repo, research, manager, proposal, session


def move_head(history, repository, name):
    branch = history.branch(repository, name)
    snapshot = history.restore(repository, branch["head_commit_id"])
    manifest = parse_contract(
        history.objects.get(branch["head_commit_id"], ObjectType.COMMIT), CommitManifest
    )
    environment = parse_contract(
        history.objects.get(manifest.environment_ref, ObjectType.ENVIRONMENT), EnvironmentManifest
    )
    writer = history.create_run(repository, name)
    history.commit(
        repository,
        writer,
        expected_head=branch["head_commit_id"],
        expected_version=branch["version"],
        state=snapshot.data,
        environment=environment,
        events=(EventInput("fixture.head-advanced", "fixture-head", {}),),
        artifacts=tuple(
            ArtifactInput(
                item.path, history.objects.get(item.object_ref, ObjectType.BLOB), item.media_type
            )
            for item in snapshot.artifacts
        ),
    )
    with history.engine.begin() as connection:
        connection.execute(
            TABLES["runs"].update().where(TABLES["runs"].c.id == writer).values(status="completed")
        )
        connection.execute(
            TABLES["branches"]
            .update()
            .where(
                TABLES["branches"].c.repository_id == repository,
                TABLES["branches"].c.name == name,
                TABLES["branches"].c.active_writer_run_id == writer,
            )
            .values(active_writer_run_id=None)
        )
