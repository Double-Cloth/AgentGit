"""真实容器崩溃测试助手；用户代码只在容器中运行。"""

import sys

from agentgit.adapters.execution.container import ExecutionIdentity, SandboxContainer
from agentgit.adapters.execution.engine import DockerEngine

identity = ExecutionIdentity(sys.argv[2], sys.argv[3], 0, 1)
container = SandboxContainer(DockerEngine.local(), sys.argv[1], identity, 7000)
container.create()
container.start()
container.ready()
container.load({})
container.execute(
    [
        "/usr/local/bin/python",
        "-I",
        "-c",
        "import subprocess,time; "
        "subprocess.Popen(['/bin/sleep','60'],start_new_session=True); time.sleep(60)",
    ]
)
