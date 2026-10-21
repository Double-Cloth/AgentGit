"""真实 Engine 的能力报告和实际导出数据发生故障时必须关闭执行入口。"""

import pytest
from test_service import call, request, result, rows

from agentgit.adapters.execution.container import SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine, EngineError
from agentgit.core.errors import ErrorCode

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


@pytest.mark.parametrize(
    "failure", ["unavailable", "windows", "cgroup1", "memory", "pids", "cpu", "seccomp"]
)
def test_unavailable_engine_or_missing_isolation_capability_never_creates_container(
    execution, failure
):
    requests = []

    class FailedCapability(DockerEngine):
        def request(self, method, path, *args, **kwargs):
            requests.append((method, path))
            if failure == "unavailable":
                raise EngineError()
            value = super().request(method, path, *args, **kwargs)
            if path == "/info":
                field, changed = {
                    "windows": ("OSType", "windows"),
                    "cgroup1": ("CgroupVersion", "1"),
                    "memory": ("MemoryLimit", False),
                    "pids": ("PidsLimit", False),
                    "cpu": ("CpuCfsQuota", False),
                    "seccomp": ("SecurityOptions", []),
                }[failure]
                value[field] = changed
            return value

    engine = FailedCapability(execution["engine"].endpoint)
    execution["service"].engine_factory = lambda: engine
    outcome = call(execution, request(execution))
    saved = result(execution, outcome)
    assert outcome.error.code == ErrorCode.SANDBOX_UNAVAILABLE
    assert saved.exit_code is None and saved.cleanup_confirmed
    assert saved.output_manifest_ref is None and not saved.integrity_complete
    assert rows(execution, "sandbox_executions")[0]["container_id"] is None
    assert all(not (method == "POST" and "/containers/create" in path) for method, path in requests)


def test_real_export_stream_truncation_is_rejected_and_container_is_cleaned(execution, monkeypatch):
    control = SandboxContainer.control

    def truncated(container, action, *args, **kwargs):
        payload = control(container, action, *args, **kwargs)
        return payload[:-1] if action == "export" else payload

    monkeypatch.setattr(SandboxContainer, "control", truncated)
    outcome = call(execution, request(execution, b"printf partial > partial.txt"))
    saved = result(execution, outcome)
    assert outcome.error.code == ErrorCode.EXPORT_INVALID
    assert saved.output_manifest_ref is None and saved.cleanup_confirmed
    row = rows(execution, "sandbox_executions")[0]
    with pytest.raises(EngineError) as missing:
        execution["engine"].request("GET", f"/containers/{row['container_id']}/json")
    assert missing.value.status == 404
    assert (
        execution["history"].branch(execution["repository"])["head_commit_id"]
        == execution["checkpoint"]
    )
