"""S3 计划、权限和文件边界的独立测试。"""

import pytest
from pydantic import ValidationError

from agentgit.adapters.corpus import Corpus
from agentgit.adapters.tools import ToolRegistry
from agentgit.adapters.workspace import ArtifactWorkspace
from agentgit.core.errors import AgentGitError
from agentgit.core.research import AgentConfig, BudgetLimits, Plan, PlanNode, ResearchGoal


def test_corpus_has_six_hashed_synthetic_sources():
    corpus = Corpus.bundled()
    assert len(corpus.sources) == 6
    assert len(corpus.search(("Aster",))) == 2
    assert all(".invalid/" in source.url for source in corpus.sources.values())
    assert all(source.trust == "untrusted_fixture" for source in corpus.sources.values())


@pytest.mark.parametrize(
    "nodes",
    [
        (PlanNode(key="a", kind="search", dependencies=("missing",)),),
        (
            PlanNode(key="a", kind="search", dependencies=("b",)),
            PlanNode(key="b", kind="search", dependencies=("a",)),
        ),
        (PlanNode(key="a", kind="search"), PlanNode(key="a", kind="search")),
        (PlanNode(key="read", kind="read"),),
    ],
)
def test_invalid_dag_rejected(nodes):
    with pytest.raises(ValidationError):
        Plan(nodes=nodes)


def test_ready_nodes_require_all_dependencies():
    plan = Plan(
        nodes=(
            PlanNode(key="search", kind="search"),
            PlanNode(key="read", kind="read", source_slot=0, dependencies=("search",)),
        )
    )
    assert [node.key for node in plan.ready(())] == ["search"]
    assert [node.key for node in plan.ready(("search",))] == ["read"]


def test_permissions_replans_and_goals_are_bounded():
    with pytest.raises(ValidationError):
        AgentConfig(tools=("terminal_exec",))
    with pytest.raises(ValidationError):
        BudgetLimits(max_replans=3)
    with pytest.raises(ValidationError):
        ResearchGoal(text="比较", frameworks=("Aster", "Aster", "Cedar"))


def workspace(tmp_path):
    return ArtifactWorkspace(tmp_path / "workspace", max_file_bytes=128, max_files=2)


def test_workspace_paths_limits_and_isolation(tmp_path):
    area = workspace(tmp_path)
    area.write("报告.md", "已验证内容", "text/markdown")
    for bad in ("../protected", "C:/protected", "dir/NUL", "x:ads"):
        with pytest.raises(AgentGitError):
            area.write(bad, "payload", "text/plain")
    with pytest.raises(AgentGitError):
        area.write("too-big", "x" * 129, "text/plain")
    assert area.contents() == {"报告.md": "已验证内容".encode()}
    other = ArtifactWorkspace(tmp_path / "other", max_file_bytes=128, max_files=2)
    other.write("报告.md", "其他 Run", "text/markdown")
    assert area.contents()["报告.md"] != other.contents()["报告.md"]


def test_workspace_links_cannot_overwrite_protected_file(tmp_path):
    area = workspace(tmp_path)
    protected = tmp_path / "protected.txt"
    protected.write_bytes(b"protected")
    link = area.root / "link.txt"
    try:
        link.symlink_to(protected)
    except OSError:
        pytest.skip("当前账户不支持符号链接")
    with pytest.raises(AgentGitError):
        area.write("link.txt", "changed", "text/plain")
    link.unlink()
    hardlink = area.root / "hard.txt"
    hardlink.hardlink_to(protected)
    area.write("hard.txt", "isolated", "text/plain")
    assert protected.read_bytes() == b"protected"
    assert area.contents()["hard.txt"] == b"isolated"


def test_unregistered_tool_and_unknown_arguments_are_rejected(tmp_path):
    tools = ToolRegistry(Corpus.bundled(), workspace(tmp_path), AgentConfig())
    with pytest.raises(AgentGitError):
        tools.invoke("terminal_exec", {"schema_version": 1, "command": "anything"})
    with pytest.raises(AgentGitError):
        tools.invoke(
            "corpus_search", {"schema_version": 1, "frameworks": ["Aster"], "bypass": True}
        )
    assert tools.executions == {}


def test_workspace_does_not_follow_linked_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "workspaces"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("当前账户不支持符号链接")
    with pytest.raises(AgentGitError):
        ArtifactWorkspace(link / "run", max_file_bytes=128, max_files=2)
    assert list(outside.iterdir()) == []
