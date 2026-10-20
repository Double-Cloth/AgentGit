"""S1-T05：数据库与目录在测试启动前拒绝重叠。"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from agentgit.config import Settings, database_url
from agentgit.core.errors import AgentGitError


@pytest.mark.parametrize(
    "dev,test", [("same", "same"), ("root", "root/nested"), ("root/nested", "root")]
)
def test_overlapping_roots_rejected(tmp_path, dev, test):
    with pytest.raises(AgentGitError, match="目录"):
        Settings(data_root=tmp_path / dev, test_data_root=tmp_path / test).assert_test_isolation()


def test_symlink_alias_rejected(tmp_path):
    dev = tmp_path / "dev"
    dev.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(dev, target_is_directory=True)
    except OSError:
        pytest.skip("当前账户不能创建符号链接；普通/包含路径保护仍已测试")
    with pytest.raises(AgentGitError):
        Settings(data_root=dev, test_data_root=alias).assert_test_isolation()


def test_database_identity_and_name():
    with pytest.raises(AgentGitError, match="测试库"):
        Settings(
            test_database_url=SecretStr(
                "postgresql+psycopg://other:secret@localhost:55432/agentgit_dev"
            )
        ).assert_test_isolation()
    with pytest.raises(AgentGitError):
        Settings(
            test_database_url=SecretStr("postgresql+psycopg://a:b@localhost/production")
        ).assert_test_isolation()
    with pytest.raises(AgentGitError):
        database_url(SecretStr("sqlite:///test.db"))
    Settings().assert_test_isolation()


def test_env_config_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("AGENTGIT_DATA_ROOT", "separate/dev")
    monkeypatch.setenv("AGENTGIT_MAX_OBJECT_BYTES", "4096")
    settings = Settings.from_env()
    assert settings.data_root == Path("separate/dev")
    assert settings.max_object_bytes == 4096
    assert "agentgit_local" not in repr(settings)
    monkeypatch.setenv("AGENTGIT_MAX_OBJECT_BYTES", "bad-secret")
    with pytest.raises(AgentGitError) as caught:
        Settings.from_env()
    assert "bad-secret" not in str(caught.value)
