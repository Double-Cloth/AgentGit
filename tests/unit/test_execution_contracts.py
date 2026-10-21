"""执行契约不能扩权、混入操作计量或接受半成功状态。"""

import pytest
from pydantic import ValidationError

from agentgit.core.errors import AgentGitError
from agentgit.core.execution import (
    CodeArguments,
    ExecutionFile,
    ExecutionManifest,
    ExecutionPolicy,
    ExecutionResult,
    TerminalArguments,
)


def arguments():
    return {"input_manifest_ref": "a" * 64, "execution_policy_ref": "b" * 64}


@pytest.mark.parametrize(
    "extra",
    [{"network": "host"}, {"pids": 100}, {"memory_bytes": 1073741824}, {"cpu_millicores": 2000}],
)
def test_policy_cannot_expand_resources(extra):
    with pytest.raises(ValidationError):
        ExecutionPolicy(image_id="sha256:" + "a" * 64, packages_digest="b" * 64, **extra)


@pytest.mark.parametrize(
    "extra",
    [
        {"image_id": "sha256:" + "c" * 64},
        {"timeout_ms": 120001},
        {"cwd": "../outside"},
        {"mount": "/root"},
    ],
)
def test_tool_arguments_cannot_change_policy(extra):
    with pytest.raises((ValidationError, AgentGitError)):
        TerminalArguments(command_ref="c" * 64, **arguments(), **extra)


def test_python_requires_exactly_one_source():
    for fields in (
        {},
        {"code_ref": "c" * 64, "script_path": "script.py"},
        {"script_path": "../script.py"},
        {"code_ref": "c" * 64, "argv": ("nul\0",)},
    ):
        with pytest.raises((ValidationError, AgentGitError)):
            CodeArguments(**arguments(), **fields)


def test_manifest_is_sorted_and_bounded():
    first = ExecutionFile(
        path="a", object_ref="a" * 64, byte_length=8388608, media_type="text/plain"
    )
    second = first.model_copy(update={"path": "b"})
    assert len(ExecutionManifest(files=(first, second)).files) == 2
    with pytest.raises(ValidationError):
        ExecutionManifest(files=(second, first))
    with pytest.raises(ValidationError):
        ExecutionManifest(files=tuple(first.model_copy(update={"path": str(i)}) for i in range(5)))


def test_success_needs_complete_export_and_confirmed_cleanup():
    values = dict(
        exit_code=0,
        error_code=None,
        stdout_ref="a" * 64,
        stderr_ref="b" * 64,
        stdout_truncated=False,
        stderr_truncated=False,
        input_manifest_ref="c" * 64,
        output_manifest_ref="d" * 64,
        image_id="sha256:" + "e" * 64,
        interpreter="/bin/sh",
        interpreter_version="dash:0.5.12-2",
        execution_policy_ref="f" * 64,
        cleanup_confirmed=True,
        integrity_complete=True,
    )
    assert ExecutionResult(**values).exit_code == 0
    for patch in (
        {"exit_code": None},
        {"cleanup_confirmed": False},
        {"integrity_complete": False},
        {"output_manifest_ref": None},
        {"container_id": "physical"},
        {"wall_ms": 100},
    ):
        with pytest.raises(ValidationError):
            ExecutionResult(**{**values, **patch})
