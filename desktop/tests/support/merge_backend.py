"""S11 桌面真实设施：复用隔离启动器，独立 Worker 只执行生产 Mock。"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import repair_backend as base
import sqlalchemy as sa
from merge_proposal_cases import archive, merge_case
from merge_proposal_verify import verify_pair
from merge_run_verify import verify_run_pair
from real_backend import source_fingerprint

from agentgit.core.canonical import ObjectType
from agentgit.core.models import parse_contract
from agentgit.core.snapshots import StateSnapshot
from agentgit.services.environment import source_revision
from agentgit.services.management import public


def frozen_files():
    paths = [
        base.ROOT / name
        for name in (
            "uv.lock",
            "pyproject.toml",
            "alembic.ini",
            "DESIGN.md",
            "desktop/package.json",
            "desktop/package-lock.json",
            "desktop/forge.config.cjs",
            "doc/AgentGit_设计文档_v1.0.0.md",
            "doc/AgentGit_实施方案_v1.0.0.md",
        )
    ]
    for folder in ("src", "migrations", "desktop/src", "desktop/.build", "desktop/tests"):
        paths.extend(
            path
            for path in (base.ROOT / folder).rglob("*")
            if path.is_file()
            and path.suffix in (".py", ".json", ".ts", ".tsx", ".css", ".js", ".html", ".ttf")
        )
    paths.extend(
        base.ROOT / "tests/integration" / name
        for name in (
            "merge_proposal_cases.py",
            "merge_proposal_verify.py",
            "merge_approval_verify.py",
            "merge_run_verify.py",
        )
    )
    paths.extend((base.ROOT / "desktop").glob("playwright*.config.ts"))
    return {
        path.relative_to(base.ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(set(paths))
    }


def seed(history, root):
    scenario = os.environ.get("AGENTGIT_MERGE_CASE", "clean")
    assert scenario in ("clean", "bad_quote")
    frozen = dict(source_revision=source_revision(), files=frozen_files(), scenario=scenario)
    (root / "merge-frozen.json").write_text(json.dumps(frozen, indent=2), "utf-8")
    case = merge_case.__wrapped__(
        history,
        root / "seed",
        SimpleNamespace(node=SimpleNamespace(name="native"), param=scenario == "bad_quote"),
    )
    repo, _, _, request, session = case
    with history.engine.connect() as connection:
        configs, tasks = base.TABLES["agent_configs"], base.TABLES["tasks"]
        config = (
            connection.execute(
                sa.select(configs)
                .join(tasks, tasks.c.agent_config_id == configs.c.id)
                .where(tasks.c.id == session.task_id)
            )
            .mappings()
            .one()
        )
    result = dict(
        repository=str(repo),
        scenario=scenario,
        request=request.model_dump(mode="json"),
        config_id=str(config["id"]),
        config_version=config["version"],
        budget=session.config.limits.model_dump(mode="json"),
        source_runs=[str(session.run_id), request.ours.source_run_id, request.theirs.source_run_id],
    )
    result["source_fingerprints"] = fingerprints(history, result)
    (root / "merge-seed.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    assert frozen["files"] == frozen_files() and source_revision() == frozen["source_revision"]
    return result


def fingerprints(history, seeded):
    return {
        run: source_fingerprint(history, UUID(seeded["repository"]), UUID(run))
        for run in seeded["source_runs"]
    }


def worker_main(schema, root):
    history = base.history_for(schema, root)
    research = base.ResearchService(history, root / "seed" / "research")
    worker = base.Worker(base.JobService(research))
    (root / "worker-ready").write_text("ready")
    try:
        while not (root / "worker-stop").exists():
            if (root / "worker-gate").exists():
                (root / "worker-gated").write_text("gated")
                time.sleep(0.03)
                continue
            (root / "worker-gated").unlink(missing_ok=True)
            worker.run_once()
            time.sleep(0.03)
    finally:
        history.engine.dispose()


def extra_command(name, request, history, root, seeded, worker):
    assert seeded is not None and worker.poll() is None
    frozen = json.loads((root / "merge-frozen.json").read_text("utf-8"))
    assert frozen["files"] == frozen_files() and frozen["source_revision"] == source_revision()
    repo = UUID(seeded["repository"])
    if name == "merge_gate":
        if request["enabled"]:
            (root / "worker-gate").write_text("controlled")
            deadline = time.monotonic() + 15
            while not (root / "worker-gated").exists():
                assert worker.poll() is None and time.monotonic() < deadline
                time.sleep(0.03)
        else:
            (root / "worker-gate").unlink(missing_ok=True)
        return dict(enabled=request["enabled"])
    if name == "merge_run":
        run = UUID(request["run"])
        with history.engine.connect() as connection:
            links = base.TABLES["run_commits"]
            checkpoints = list(
                connection.execute(
                    sa.select(links.c.commit_id)
                    .where(links.c.repository_id == repo, links.c.run_id == run)
                    .order_by(links.c.run_order)
                ).scalars()
            )
            runs, jobs = base.TABLES["merge_run_requests"], base.TABLES["jobs"]
            row = (
                connection.execute(
                    sa.select(runs)
                    .join(jobs, jobs.c.id == runs.c.job_id)
                    .where(runs.c.repository_id == repo, jobs.c.run_id == run)
                )
                .mappings()
                .one()
            )
        initial = parse_contract(
            history.objects.get(row["initial_state_ref"], ObjectType.STATE), StateSnapshot
        )
        return dict(checkpoints=checkpoints, initial=initial.model_dump(mode="json"))
    with history.engine.connect() as connection:
        counts = {
            name: connection.execute(
                sa.select(sa.func.count()).select_from(base.TABLES[name])
            ).scalar_one()
            for name in (
                "jobs",
                "runs",
                "tasks",
                "external_calls",
                "merge_proposals",
                "merge_revisions",
                "merge_review_sessions",
                "merge_approvals",
                "merge_rejections",
                "agent_configs",
            )
        }
    state = dict(counts=counts, source_fingerprints=fingerprints(history, seeded))
    assert state["source_fingerprints"] == seeded["source_fingerprints"]
    if name == "merge_state":
        return state
    assert name == "merge_archive", "未知合并联调命令"
    label = request["label"]
    assert label in ("approved", "final", "rejected")
    details = public(dict(state=state))
    archive(history, repo, root / "seed", "native/" + label, details)
    evidence = root / "seed/evidence/native"
    if label == "final":
        source = json.loads((evidence / "source/evidence.json").read_text("utf-8"))
        final = json.loads((evidence / "final/evidence.json").read_text("utf-8"))
        audit = verify_run_pair(
            source, final, frozen["source_revision"], frozen["files"]["uv.lock"]
        )
        (root / "merge-raw-audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), "utf-8"
        )
        state["raw_audit"] = audit
    elif label == "rejected":
        source = json.loads((evidence / "source/evidence.json").read_text("utf-8"))
        final = json.loads((evidence / "rejected/evidence.json").read_text("utf-8"))
        audit = verify_pair(source, final, frozen["source_revision"], frozen["files"]["uv.lock"])
        (root / "merge-rejection-audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), "utf-8"
        )
        state["raw_audit"] = audit
    return dict(
        **state,
        evidence=str(evidence / label / "evidence.json"),
        frozen=str(root / "merge-frozen.json"),
    )


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        base.main(seed_case=seed, extra_command=extra_command)
