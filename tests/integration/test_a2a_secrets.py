"""真实 CAS 前拒绝凭据输入，包含尚未登记到数据库的物理对象。"""

import copy
import os

import pytest
from test_a2a_fixture import CASES
from test_a2a_fixture import server as server
from test_a2a_gateway import approve, prepared
from test_a2a_gateway import archive as archive

from agentgit.core.a2a_wire import A2ACall, A2AResult
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("location", ["body", "reference_context"])
def test_exact_secret_is_rejected_before_gateway_or_physical_cas(history, server, location):
    values = prepared(history, server)
    adapter, gateway, approval, repo, run, _, state, observed = values
    secret = os.environ[observed["credential_envs"]["owner"]]
    body = copy.deepcopy(CASES["request"])
    call = A2ACall(operation="send", body_json=encode(body).decode())
    if location == "body":
        body["message"]["parts"][0]["text"] = secret
        call = A2ACall(operation="send", body_json=encode(body).decode())
    else:
        approve(history, values, call, "public-send")
        result = parse_contract(
            adapter.call(gateway, step="public-send", attempt=1, call=call).payload, A2AResult
        )
        reference = result.reference().model_copy(update={"context_id": secret})
        call = A2ACall(
            operation="get", body_json=encode({"id": reference.task_id}).decode(), task=reference
        )
    before_requests = len(state()["requests"])
    before_approvals = approval.list_for_run(repo, run)
    before_files = {
        p.relative_to(history.objects.root): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }
    with pytest.raises(AgentGitError) as error:
        adapter.call(gateway, step="private-rejected", attempt=1, call=call)
    assert error.value.code == ErrorCode.FORBIDDEN and secret not in str(error.value)
    assert len(state()["requests"]) == before_requests
    assert approval.list_for_run(repo, run) == before_approvals
    after_files = {
        p.relative_to(history.objects.root): p.read_bytes()
        for p in history.objects.root.rglob("*")
        if p.is_file()
    }
    assert after_files == before_files
    assert all(secret.encode() not in raw for raw in after_files.values())
