"""真实观测补丁桌面联调，复用已冻结的输入顺序故障及只读录制。"""

import hashlib
import sys
import time
from pathlib import Path

import repair_backend as base
import sqlalchemy as sa
from test_repair_observation_falsefix import (
    ObservationOrderModel,
    ReversedSearchRecording,
    binding,
)

from agentgit.core.models import parse_contract
from agentgit.core.research import ModelRequest, ResearchGoal
from agentgit.repair.policy import mock_budget
from agentgit.services.validators import ValidatorService


def policy():
    return (
        binding()
        + ";desktop-base-sha256="
        + hashlib.sha256(Path(base.__file__).read_bytes()).hexdigest()
        + ";desktop-observation-sha256="
        + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    )


class FrozenObservationModel(ObservationOrderModel):
    def invoke(self, payload):
        request = parse_contract(payload, ModelRequest)
        assert policy() in request.state.goal.text
        return super().invoke(payload)


def seed(history, root):
    research = base.ResearchService(history, root / "seed" / "research")
    repo = history.create_repository("真实桌面观测补丁范围")
    config = research.create_config(repo)
    donor = research.start(
        repo, research.create_task(repo, config, ResearchGoal(text="observation-donor;" + policy()))
    )
    donor.tools = ReversedSearchRecording(research.corpus, donor.workspace, donor.config)
    assert donor.execute(FrozenObservationModel()).status.value == "completed"
    failed = research.start(
        repo,
        research.create_task(
            repo, config, ResearchGoal(text="observation-order-fault;" + policy())
        ),
    )
    assert failed.execute(FrozenObservationModel()).status.value == "failed"
    validator = ValidatorService(history).freeze(repo, failed.state.goal, research.corpus)
    tables = base.TABLES
    with history.engine.connect() as connection:
        calls, events, links = (
            tables[name] for name in ("external_calls", "events", "run_commits")
        )
        original = (
            connection.execute(
                sa.select(calls).where(
                    calls.c.run_id == failed.run_id, calls.c.provider == "tool:corpus_search/v1"
                )
            )
            .mappings()
            .one()
        )
        replacement = (
            connection.execute(
                sa.select(calls).where(
                    calls.c.run_id == donor.run_id, calls.c.provider == "tool:corpus_search/v1"
                )
            )
            .mappings()
            .one()
        )
        event = connection.execute(
            sa.select(events.c.id).where(
                events.c.run_id == failed.run_id,
                events.c.logical_step_id == original["logical_step_id"],
                events.c.type == "tool.started",
            )
        ).scalar_one()
        first = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == failed.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    regression = []
    for index in range(4):
        task = research.create_task(
            repo, config, ResearchGoal(text=f"regression-{index};{policy()}")
        )
        run = research.start(repo, task)
        assert run.execute(FrozenObservationModel()).status.value == "completed"
        with history.engine.connect() as connection:
            checkpoint = connection.execute(
                sa.select(links.c.commit_id)
                .where(links.c.run_id == run.run_id)
                .order_by(links.c.run_order)
                .limit(1)
            ).scalar_one()
        regression.append(dict(source_run_id=str(run.run_id), from_commit=checkpoint))
    return dict(
        repository=str(repo),
        failed_run=str(failed.run_id),
        base=first,
        validator=validator,
        target_event=event,
        replacement_call_id=str(replacement["id"]),
        hybrid=dict(schema_version=1, until_commit=failed.head, calls=[str(original["id"])]),
        budget=mock_budget(failed.config).model_dump(mode="json"),
        regression=regression,
        source_fingerprint=base.source_fingerprint(history, repo, failed.run_id),
        policy=policy(),
    )


def worker_main(schema, root):
    history = base.history_for(schema, root)
    research = base.ResearchService(history, root / "seed" / "research")
    worker = base.Worker(base.JobService(research), model_factory=FrozenObservationModel)
    controllers = [
        base.RepairSearchWorker(research),
        base.RepairStudyWorker(research),
        base.RepairConfirmationWorker(research),
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


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2], Path(sys.argv[3]))
    else:
        base.main(seed_case=seed, evidence_case="S10-real-electron-observation/v1")
