"""模板发布必须经过真实 S7 校验；独立耐久试验验证缺失产物修复。"""

from uuid import UUID

import pytest
from test_debug_candidate_experiments import candidate_case as candidate_case
from test_recorded_replayer import replay_source as replay_source

from agentgit.core.canonical import encode
from agentgit.core.experiments import ExperimentGroup
from agentgit.core.repair import RepairBinding
from agentgit.core.research import AgentConfig, evolve
from agentgit.core.snapshots import StateSnapshot
from agentgit.repair.space import enumerate_proposals, freeze_space
from agentgit.repair.templates import TemplateAnchor, model_templates
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_generated_artifact_template_publishes_and_repairs_real_failed_run(history, candidate_case):
    session, manager, report_id, event_id, bound, request = candidate_case
    repo = session.repository_id
    context = bound["context"]
    view = manager.checkout(repo, context["base_commit"])
    snapshot = StateSnapshot.model_validate_json(encode(view["snapshot"]))
    config = AgentConfig.model_validate_json(encode(context["config"]))
    anchor = TemplateAnchor.model_validate_json(
        encode(
            {
                **{
                    key: context[key]
                    for key in (
                        "source_run_id",
                        "base_commit",
                        "target_commit_id",
                        "target_event_id",
                        "precondition",
                    )
                },
            }
        )
    )
    generated = model_templates(anchor, config, snapshot, history.objects.get)
    assert [patch.template for patch in generated.patches] == [
        "prompt-verified-sources/v1",
        "prompt-required-artifacts/v1",
        "plan-required-artifacts/v1",
    ]
    binding = RepairBinding(
        repository_id=str(repo),
        failed_run_id=anchor.source_run_id,
        base_commit=anchor.base_commit,
        environment_ref=view["manifest"]["environment_ref"],
        validator_ref=anchor.precondition.validator_ref,
    )
    space = freeze_space(
        binding, generated.patches, source_reason="S8 注入点定位未达标，完整候选降级"
    )
    assert len(list(enumerate_proposals(space))) == 7
    branch = history.branch(repo)
    source_files = session.workspace.contents()
    original_report = manager.debug_report(repo, report_id)
    persisted = [
        manager.create_intervention(repo, patch.intervention, f"template-{index}")
        for index, patch in enumerate(generated.patches)
    ]
    assert len({item["intervention_id"] for item in persisted}) == 3
    submitted = manager.create_experiment(
        repo,
        evolve(
            request,
            repeats=1,
            groups=(
                ExperimentGroup(key="A", label="缺失来源产物"),
                ExperimentGroup(
                    key="B",
                    label="自动有限模板补入产物",
                    interventions=(persisted[2]["intervention_id"],),
                ),
            ),
        ),
        "template-artifact-check",
    )
    worker = Worker(manager.jobs)
    assert sorted(worker.run_once()["status"] for _ in range(2)) == ["completed", "failed"]
    assert worker.run_once() is None
    result = manager.experiment(repo, UUID(submitted["experiment_id"]))
    assert {item["group"]: item["verdict"] for item in result["trials"]} == {
        "A": "failed",
        "B": "passed",
    }
    assert len({item["run_id"] for item in result["trials"]}) == 2
    assert all(item["run_id"] != anchor.source_run_id for item in result["trials"])
    assert history.branch(repo) == branch
    assert session.workspace.contents() == source_files
    assert manager.debug_report(repo, report_id) == original_report
