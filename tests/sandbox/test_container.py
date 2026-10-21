"""真实 Shell/Python、双身份采集、断网和资源边界；不冒充完整 S4 验收。"""

import json
import time

import pytest

from agentgit.adapters.execution.engine import EngineError
from agentgit.core.errors import AgentGitError, ErrorCode

pytestmark = pytest.mark.sandbox


def shell(container, command, **kwargs):
    return container.execute(["/bin/sh", "-c", command], **kwargs)


def test_shell_pipeline_script_git_and_binary(container_factory):
    container = container_factory(
        {"input": b"alpha\nbeta\n", "run.sh": b"#!/bin/sh\nprintf script"}
    )
    output = shell(
        container,
        "./run.sh; cat input | rg alpha | tr a-z A-Z > result; "
        "mkdir repo; cd repo; git init -q; git config user.email fixture@example.invalid; "
        "git config user.name fixture; printf value > tracked; git add tracked; "
        "git commit -qm first; git log -1 --format=%s; printf '\\377' >&2",
    )
    assert output.exit_code == 0
    assert output.stdout == b"scriptfirst\n"
    assert output.stderr == b"\xff"
    files, receipt = container.export()
    assert files["result"] == b"ALPHA\n"
    assert files["repo/tracked"] == b"value"
    assert "repo/.git/HEAD" in files
    assert receipt["complete"] is True


def test_python_argv_stdin_and_json_script(container_factory):
    code = (
        "import json,sys,pathlib\n"
        "data=json.load(sys.stdin)\n"
        "pathlib.Path('result.json').write_text(json.dumps({'sum':sum(data),'arg':sys.argv[1]}))\n"
        "print(sys.version_info[:2])\n"
    )
    container = container_factory({"calculate.py": code.encode()})
    output = container.execute(
        ["/usr/local/bin/python", "-I", "calculate.py", "参数"], stdin=b"[1, 2, 3]"
    )
    assert output.exit_code == 0
    assert output.stdout == b"(3, 12)\n"
    files, _ = container.export()
    assert json.loads(files["result.json"]) == {"sum": 6, "arg": "参数"}


@pytest.mark.parametrize(
    ("command", "exit_code"),
    [
        (["/bin/sh", "-c", "exit 23"], 23),
        (["/bin/sh", "-c", "agentgit_command_does_not_exist"], 127),
        (["/usr/local/bin/python", "-I", "-c", "raise RuntimeError('fixture')"], 1),
        (["/usr/local/bin/python", "-I", "-c", "if invalid"], 1),
    ],
)
def test_real_exit_code(container_factory, command, exit_code):
    output = container_factory().execute(command)
    assert output.exit_code == exit_code
    if exit_code != 23:
        assert output.stderr


def test_same_path_isolation_and_deletion(container_factory):
    first = container_factory({"shared": b"first", "deleted": b"old"})
    second = container_factory({"shared": b"second"})
    assert shell(first, "printf changed > shared; rm deleted").exit_code == 0
    assert shell(second, "cat shared").stdout == b"second"
    assert first.export()[0] == {"shared": b"changed"}
    assert second.export()[0] == {"shared": b"second"}


@pytest.mark.parametrize(
    "command",
    [
        "ln -s /etc/passwd link",
        "printf x > a; ln a b",
        "mkfifo fifo",
        "mkdir CON",
        "mkdir hidden; printf secret > hidden/file; chmod 700 hidden",
        "printf secret > hidden; chmod 600 hidden",
        "printf bad > 'C:stream'",
    ],
)
def test_invalid_exports(container_factory, command):
    container = container_factory()
    assert shell(container, command).exit_code == 0
    with pytest.raises(AgentGitError):
        container.export()


def test_stdout_stderr_are_bounded_original_bytes(container_factory):
    container = container_factory()
    output = container.execute(
        [
            "/usr/local/bin/python",
            "-I",
            "-c",
            "import os; os.write(1,b'\\xff'*2000000); os.write(2,b'\\xfe'*2000000)",
        ]
    )
    assert output.exit_code == 0
    assert output.stdout == b"\xff" * 1048576
    assert output.stderr == b"\xfe" * 1048576
    assert output.stdout_truncated and output.stderr_truncated
    assert container.inspect()["HostConfig"]["LogConfig"]["Type"] == "none"


def test_file_count_limit(container_factory):
    container = container_factory()
    assert (
        container.execute(
            [
                "/usr/local/bin/python",
                "-I",
                "-c",
                "from pathlib import Path; [Path(str(i)).touch() for i in range(1001)]",
            ]
        ).exit_code
        == 0
    )
    with pytest.raises(AgentGitError) as caught:
        container.export()
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT


@pytest.mark.parametrize(("count", "size"), [(1, 9 * 1048576), (5, 7 * 1048576)])
def test_file_and_total_export_limit(container_factory, count, size):
    container = container_factory()
    code = (
        f"from pathlib import Path; [Path(str(i)).write_bytes(b'x'*{size}) for i in range({count})]"
    )
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 0
    with pytest.raises(AgentGitError) as caught:
        container.export()
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT


def test_collector_disappearance_cannot_export(container_factory):
    container = container_factory()
    assert shell(container, "printf partial > partial").exit_code == 0
    container.engine.request("POST", f"/containers/{container.container_id}/kill")
    with pytest.raises(AgentGitError):
        container.export()


def test_empty_directory_count_is_bounded(container_factory):
    container = container_factory()
    output = container.execute(
        [
            "/usr/local/bin/python",
            "-I",
            "-c",
            "from pathlib import Path; [Path(str(i)).mkdir() for i in range(4001)]",
        ]
    )
    assert output.exit_code == 0
    with pytest.raises(AgentGitError) as caught:
        container.export()
    assert caught.value.code == ErrorCode.OUTPUT_LIMIT


def test_security_settings_and_no_host_environment(container_factory, engine, monkeypatch):
    monkeypatch.setenv("AGENTGIT_HOST_SECRET", "synthetic-do-not-inherit")
    monkeypatch.setenv("DOCKER_CONTEXT", "agentgit-nonexistent-test-context")
    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")
    container = container_factory()
    settings = container.inspect()
    assert settings["Config"]["User"] == "10001:10003"
    image_env = engine.request("GET", f"/images/{container.image_id}/json")["Config"]["Env"]
    expected = dict(item.split("=", 1) for item in image_env)
    expected.update(PATH="/usr/local/bin:/usr/bin:/bin", HOME="/tmp", LANG="C.UTF-8")
    assert dict(item.split("=", 1) for item in settings["Config"]["Env"]) == expected
    assert "AGENTGIT_HOST_SECRET" not in expected
    host = settings["HostConfig"]
    assert host["ReadonlyRootfs"] is True
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert host["NetworkMode"] == "none"
    assert host["PidMode"] != "host"
    assert host["IpcMode"] == "private" and host["CgroupnsMode"] == "private"
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert not host["Binds"] and not host["Devices"]
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024 * 1024
    assert host["NanoCpus"] == 1000000000 and host["PidsLimit"] == 64
    output = container.execute(
        [
            "/usr/local/bin/python",
            "-I",
            "-c",
            "import os,pathlib,socket; "
            "assert os.getuid()==10002; "
            "assert not pathlib.Path('/var/run/docker.sock').exists(); "
            "assert not os.access('/root/.docker/config.json',os.R_OK); "
            "assert set(os.environ)=={'PATH','HOME','LANG'}; "
            "print(pathlib.Path('/proc/self/status').read_text())",
        ]
    )
    assert output.exit_code == 0, output.stderr
    assert b"NoNewPrivs:\t1" in output.stdout
    assert b"Seccomp:\t2" in output.stdout
    assert b"CapEff:\t0000000000000000" in output.stdout


def test_network_and_root_write_are_denied(container_factory):
    container = container_factory()
    code = """import socket,pathlib
for address in [('1.1.1.1',443),('127.0.0.1',55432),('172.17.0.1',55432)]:
    try:
        socket.create_connection(address,timeout=.2)
    except OSError:
        pass
    else:
        raise AssertionError('network reachable')
try:
    pathlib.Path('/etc/agentgit-test').write_text('bad')
except OSError:
    pass
else:
    raise AssertionError('root writable')
print('isolated')
"""
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 0, output.stderr
    assert output.stdout == b"isolated\n"


def test_cannot_signal_collector_or_forge_protocol(container_factory):
    container = container_factory()
    code = """import os,signal,socket
try:
    os.kill(1,signal.SIGKILL)
except PermissionError:
    pass
else:
    raise AssertionError('collector kill allowed')
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.connect('\\0agentgit-collector-v1')
try:
    s.sendall(b'forged success')
    assert s.recv(16)==b''
except (ConnectionResetError,BrokenPipeError):
    pass
print('{"complete":true,"exit_code":0}')
raise SystemExit(19)
"""
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 19, output.stderr
    assert b'"complete":true' in output.stdout
    assert container.export()[0] == {}


def test_background_process_blocks_export(container_factory):
    container = container_factory()
    output = shell(container, "sleep 20 </dev/null >/dev/null 2>&1 & printf foreground")
    assert output.exit_code == 0
    with pytest.raises(AgentGitError):
        container.export()


def test_independent_pid1_deadline(container_factory):
    container = container_factory(timeout_ms=4000)
    assert shell(container, "sleep 20 </dev/null >/dev/null 2>&1 &").exit_code == 0
    start = time.monotonic()
    # 此后不再发送停止请求；PID 1 自行终止整个 namespace。
    time.sleep(4.2)
    state = container.inspect()["State"]
    assert state["Running"] is False
    assert state["ExitCode"] == 124
    assert time.monotonic() - start < 6


def test_missing_digest_does_not_pull_or_fallback(engine):
    with pytest.raises(EngineError) as caught:
        engine.request("GET", "/images/sha256:" + "f" * 64 + "/json")
    assert caught.value.status == 404
