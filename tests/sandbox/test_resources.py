"""真实触达 cgroup 与 tmpfs 限制；不能用退出码或用户自报替代资源证据。"""

import pytest

pytestmark = pytest.mark.sandbox


def test_memory_limit_has_cgroup_evidence(container_factory):
    container = container_factory()
    output = container.execute(
        ["/usr/local/bin/python", "-I", "-c", "data=bytearray(700*1024*1024)"]
    )
    assert output.exit_code != 0
    state = container.inspect()["State"]
    if state["Running"]:
        assert container.ready()["counters"]["memory.events:oom_kill"] > 0
    else:
        assert state["OOMKilled"] is True


def test_pids_limit_has_cgroup_evidence(container_factory):
    container = container_factory()
    code = """import subprocess,errno
children=[]
try:
    for _ in range(100):
        try:
            children.append(subprocess.Popen(['/bin/sleep','15']))
        except OSError as error:
            assert error.errno==errno.EAGAIN
            break
    else:
        raise AssertionError('pids limit absent')
    assert 1<len(children)<64
finally:
    for child in children:
        child.terminate()
    for child in children:
        child.wait()
"""
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 0, output.stderr
    assert container.ready()["counters"]["pids.events:max"] > 0
    assert container.export()[0] == {}


def test_workspace_and_tmp_capacity(container_factory):
    container = container_factory()
    code = """import os,errno
for directory,limit in [('/workspace',128*1024*1024),('/tmp',32*1024*1024)]:
    stat=os.statvfs(directory)
    assert stat.f_blocks*stat.f_frsize==limit
    try:
        with open(directory+'/full','wb') as target:
            for _ in range(limit//1048576+1):
                target.write(b'x'*1048576)
    except OSError as error:
        assert error.errno==errno.ENOSPC
    else:
        raise AssertionError('tmpfs size not enforced')
    os.unlink(directory+'/full')
"""
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 0, output.stderr
    assert container.export()[0] == {}


def test_cpu_quota_is_enforced_by_kernel(container_factory):
    container = container_factory()
    before = container.ready()["counters"]
    code = """import subprocess,sys,pathlib
quota,period=map(int,pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().split())
assert quota==period
program='import time; end=time.monotonic()+2\\nwhile time.monotonic()<end: pass'
children=[subprocess.Popen([sys.executable,'-I','-c',program]) for _ in range(2)]
for child in children:
    assert child.wait()==0
"""
    output = container.execute(["/usr/local/bin/python", "-I", "-c", code])
    assert output.exit_code == 0, output.stderr
    after = container.ready()["counters"]
    assert after["cpu.stat:nr_throttled"] > before["cpu.stat:nr_throttled"]
    assert after["cpu.stat:throttled_usec"] > before["cpu.stat:throttled_usec"]
