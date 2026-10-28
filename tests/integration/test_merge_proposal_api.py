"""HTTP 与 CLI 共用耐久提案服务；查询只读，缺陷/旧版本输入明确拒绝。"""

import json
from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from merge_proposal_cases import archive, history_fingerprint
from merge_proposal_cases import merge_case as merge_case
from pydantic import SecretStr
from test_merge_proposals import expected, forbid_execution
from typer.testing import CliRunner

from agentgit.api.main import create_app
from agentgit.cli.main import app
from agentgit.config import Settings
from agentgit.core.canonical import encode
from agentgit.core.merge_proposals import (
    MergeEdits,
    PrepareMergeReview,
    RejectMergeProposal,
    ReplaceMergeRevision,
    ValidateMergeRevision,
)

pytestmark = pytest.mark.integration
TOKEN = "merge-local-test-token-1234567890"


def test_http_cli_reopen_and_exact_inputs_have_no_execution(
    history, merge_case, tmp_path, monkeypatch
):
    repo, _, manager, request, _ = merge_case
    forbid_execution(monkeypatch)
    before = history_fingerprint(history, repo)

    @contextmanager
    def management(testing):
        yield manager

    monkeypatch.setattr("agentgit.cli.main.management", management)
    runner = CliRunner()

    def invoke(command, body=None, key=None, proposal=None):
        args = [command, str(repo)]
        if proposal is not None:
            args.append(proposal)
        if body is not None:
            file = tmp_path / (command + ".json")
            file.write_bytes(encode(body))
            args += ["--file", str(file)]
        if key is not None:
            args += ["--key", key]
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout)

    with TestClient(
        create_app(Settings(api_token=SecretStr(TOKEN)), management=manager),
        base_url="http://127.0.0.1",
        headers={"Authorization": "Bearer " + TOKEN, "X-Repository-ID": str(repo)},
    ) as client:
        response = client.post(
            "/merge-proposals",
            json=request.model_dump(mode="json"),
            headers={"Idempotency-Key": "create"},
        )
        assert response.status_code == 201, response.text
        created = response.json()
        identity = created["proposal_id"]
        url = "/merge-proposals/" + identity
        assert invoke("merge-propose", request.model_dump(mode="json"), "create") == created

        def forbidden(*args, **kwargs):
            pytest.fail("查询不能发布 CAS")

        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            assert invoke("merge-status", proposal=identity) == client.get(url).json()
            assert client.get(url, headers={"X-Repository-ID": str(uuid4())}).status_code == 404
        for payload, status in (
            ({}, 409),
            ({"schema_version": 2}, 409),
            ({"schema_version": 1, "passed": True}, 422),
        ):
            rejected = client.post(
                url + "/validate", json=payload, headers={"Idempotency-Key": "invalid"}
            )
            assert rejected.status_code == status, rejected.text
        checked = client.post(
            url + "/validate",
            json=ValidateMergeRevision(**expected(created)).model_dump(mode="json"),
            headers={"Idempotency-Key": "validate-1"},
        )
        assert checked.status_code == 201, checked.text
        validation = checked.json()
        ticket = client.post(
            url + "/review",
            json=PrepareMergeReview(
                **expected(created), validation_id=validation["validation_id"]
            ).model_dump(mode="json"),
            headers={"Idempotency-Key": "review-1"},
        )
        assert ticket.status_code == 201, ticket.text
        changed = invoke(
            "merge-edit",
            ReplaceMergeRevision(**expected(created), edits=MergeEdits()).model_dump(mode="json"),
            "edit",
            identity,
        )
        assert (
            client.post(
                url + "/review",
                json=PrepareMergeReview(
                    **expected(created), validation_id=validation["validation_id"]
                ).model_dump(mode="json"),
                headers={"Idempotency-Key": "stale-review"},
            ).status_code
            == 409
        )
        new_validation = invoke(
            "merge-validate",
            ValidateMergeRevision(**expected(changed)).model_dump(mode="json"),
            "validate-2",
            identity,
        )
        new_ticket = invoke(
            "merge-review",
            PrepareMergeReview(
                **expected(changed), validation_id=new_validation["validation_id"]
            ).model_dump(mode="json"),
            "review-2",
            identity,
        )
        assert new_ticket["ticket"]["revision_ref"] == changed["revision_ref"]
        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            reopened = client.get(url).json()
            assert (
                len(reopened["reviews"]) == 2
                and sum(item["current"] for item in reopened["reviews"]) == 1
            )
            assert invoke("merge-status", proposal=identity) == reopened
            exact = client.get(url + "/reviews/" + new_ticket["review_id"])
            assert exact.status_code == 200 and exact.json()["current"]
            result = runner.invoke(
                app, ["merge-review-status", str(repo), identity, new_ticket["review_id"]]
            )
            assert result.exit_code == 0 and json.loads(result.stdout) == exact.json()
            older = client.get(url, params={"review_before": new_ticket["review_id"]}).json()
            assert len(older["reviews"]) == 1 and not older["reviews"][0]["current"]
            assert client.get(url + "/reviews/" + str(uuid4())).status_code == 404
        decision = invoke(
            "merge-reject",
            RejectMergeProposal(**expected(changed), reason="入口验收拒绝").model_dump(mode="json"),
            "reject",
            identity,
        )
        assert client.get(url).json()["rejection"] == decision["decision"]
        assert (
            client.post(
                url + "/approve",
                json={"schema_version": 1},
                headers={"Idempotency-Key": "no-approve"},
            ).status_code
            == 404
        )
    assert history_fingerprint(history, repo) == before
    archive(
        history,
        repo,
        tmp_path,
        "api-cli/final",
        dict(
            created=created,
            changed=changed,
            validation=new_validation,
            ticket=new_ticket,
            decision=decision,
            view=manager.merge_proposal(repo, UUID(identity)),
        ),
    )
