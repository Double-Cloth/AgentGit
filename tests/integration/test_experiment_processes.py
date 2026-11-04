"""真实 TCP API 重启后，由独立 CLI Worker 完成十个实验 trial。"""

import json
import os
import subprocess
import sys

import httpx
import pytest
import sqlalchemy as sa
from test_management_api import TOKEN
from test_management_processes import server

from agentgit.config import Settings, database_url
from agentgit.core.canonical import encode
from agentgit.core.experiments import ExperimentGroup, NewExperiment
from agentgit.core.hybrid import HybridPolicy
from agentgit.core.research import BudgetLimits, ResearchGoal
from agentgit.services.management import ManagementService
from agentgit.services.research import ResearchService
from agentgit.storage.schema import build_metadata

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["live", "hybrid"])
def test_real_api_restart_and_separate_cli_workers_complete_ab_five(history, tmp_path, mode):
    research = ResearchService(history, tmp_path)
    assert history.objects.root == research.data_root / "objects"
    repo = history.create_repository("独立进程实验")
    config = research.create_config(repo)
    task = research.create_task(repo, config, ResearchGoal(text="比较三个 fixture 框架"))
    source = research.start(repo, task)
    source.execute()
    manager = ManagementService(research)
    commits = manager.commits(repo)["items"]
    checkpoint = list(reversed(commits))[3 if mode == "hybrid" else 15]["commit_id"]
    hybrid = None
    if mode == "hybrid":
        calls = build_metadata().tables["external_calls"]
        with history.engine.connect() as connection:
            selected = connection.execute(
                sa.select(calls.c.id).where(
                    calls.c.run_id == source.run_id, calls.c.logical_step_id == "read-0"
                )
            ).scalar_one()
        hybrid = HybridPolicy(until_commit=source.head, calls=(str(selected),))
    original = history.branch(repo)
    headers = {"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)}
    with (
        server(manager, tmp_path / "api-first.json") as (url, schema),
        httpx.Client(base_url=url, headers=headers, timeout=30) as client,
    ):
        frozen = client.post(
            "/validators",
            json={
                "schema_version": 1,
                "source_run_id": str(source.run_id),
                "from_commit": checkpoint,
            },
            headers={"Idempotency-Key": "process-validator"},
        )
        assert frozen.status_code == 201, frozen.text
        request = NewExperiment(
            source_run_id=str(source.run_id),
            from_commit=checkpoint,
            validator_ref=frozen.json()["validator_ref"],
            budget=BudgetLimits(),
            hybrid=hybrid,
            groups=(
                ExperimentGroup(key="A", label="基线"),
                ExperimentGroup(
                    key="B", label="核验引用", prompt=source.config.prompt + "核验所有引用。"
                ),
            ),
        )
        request_path = tmp_path / "experiment.json"
        request_path.write_bytes(encode(request.model_dump(mode="json")))
        isolated_url = database_url(Settings.from_env().test_database_url).update_query_dict(
            {
                "options": f"-csearch_path={schema}",
            }
        )
        environment = {
            **os.environ,
            "AGENTGIT_TEST_DATABASE_URL": isolated_url.render_as_string(hide_password=False),
            "AGENTGIT_TEST_DATA_ROOT": str(research.data_root),
            "AGENTGIT_OPERATOR_ID": manager.actor,
        }

        def cli(*arguments):
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from agentgit.cli.main import app; app()",
                    *arguments,
                    "--testing",
                ],
                capture_output=True,
                timeout=30,
                check=False,
                env=environment,
            )
            assert result.returncode == 0, result.stderr.decode(errors="replace")
            return json.loads(result.stdout)

        submitted = cli(
            "experiment", str(repo), "--file", str(request_path), "--key", "process-experiment"
        )
        replayed = client.post(
            "/experiments",
            json=request.model_dump(mode="json"),
            headers={
                "Idempotency-Key": "process-experiment",
            },
        )
        assert replayed.status_code == 202, replayed.text
        assert replayed.json() == submitted
        before = client.get(f"/experiments/{submitted['experiment_id']}").json()
        assert len(before["trials"]) == 10
        assert all(trial["status"] == "queued" for trial in before["trials"])
        assert all(group["passed"] == 0 for group in before["groups"])
    # API 已退出，队列仍由各自独立的新 CLI Worker 进程处理。
    actual = [cli("worker", "--once") for _ in range(10)]
    assert all(result["status"] == "completed" for result in actual)
    assert cli("worker", "--once")["status"] == "idle"
    with (
        server(manager, tmp_path / "api-second.json") as (url, _),
        httpx.Client(base_url=url, headers=headers, timeout=30) as client,
    ):
        response = client.get(f"/experiments/{submitted['experiment_id']}")
        assert response.status_code == 200, response.text
        report = response.json()
        assert report == cli("experiment-status", str(repo), submitted["experiment_id"])
        assert report["groups"] == [
            {"key": key, "total": 5, "passed": 5, "incomplete": 0} for key in ("A", "B")
        ]
        assert len({trial["run_id"] for trial in report["trials"]}) == 10
        expected_model, expected_tools = (9, 7) if mode == "hybrid" else (3, 2)
        assert all(
            trial["replay"]["actual"]["model_calls"] == expected_model for trial in report["trials"]
        )
        assert all(
            trial["replay"]["actual"]["tool_calls"] == expected_tools for trial in report["trials"]
        )
        if mode == "hybrid":
            assert report["test_only"]
            for trial in report["trials"]:
                replay = trial["replay"]
                assert replay["mode"] == "hybrid"
                assert replay["actual"]["recorded_observations"] == 1
                assert replay["observations"][0]["selected_call_id"] == str(selected)
        assert all(trial["replay"]["actual"]["unknown_outcomes"] == 0 for trial in report["trials"])
        assert report["manifest"]["request"]["validator_ref"] == request.validator_ref
        (tmp_path / "experiment-process-audit.json").write_bytes(encode(report))
    assert history.branch(repo) == original
