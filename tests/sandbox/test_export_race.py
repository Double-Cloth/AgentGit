"""在实际 Linux 工作区注入 stat 与读取间的变化，不能得到成功快照。"""

import pytest

pytestmark = pytest.mark.sandbox


def test_collector_rejects_file_changed_after_metadata_scan(container_factory):
    container = container_factory()
    output = container.execute(["/bin/sh", "-c", "printf before > changing.txt"])
    assert output.exit_code == 0
    program = """from agentgit.adapters.execution import supervisor
from agentgit.core.errors import AgentGitError
scan = supervisor._workspace_entries
def changed():
    for path, metadata in scan():
        if path.name == 'changing.txt':
            path.unlink()
            path.write_bytes(b'changed-after-stat')
        yield path, metadata
supervisor._workspace_entries = changed
try:
    supervisor.snapshot()
except AgentGitError as error:
    assert error.code.value == 'validation'
    print('change rejected')
else:
    raise AssertionError('changed export accepted')
"""
    # 故障只注入可信测试进程的扫描函数；不修改固定镜像或用户执行身份。
    checked = container.engine.exec(
        container.container_id,
        ["/usr/local/bin/python", "-I", "-c", program],
        user="10001:10003",
        output_limit=4096,
    )
    assert checked.exit_code == 0, checked.stderr
    assert checked.stdout == b"change rejected\n"
