"""显式配置、秘密包装及开发/测试数据隔离。"""

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from agentgit.core.errors import AgentGitError, ErrorCode


def database_url(value: SecretStr) -> URL:
    try:
        url = make_url(value.get_secret_value())
    except ArgumentError:
        raise AgentGitError(ErrorCode.CONFIGURATION, "数据库 URL 格式无效") from None
    if url.drivername != "postgresql+psycopg" or not url.database:
        raise AgentGitError(ErrorCode.CONFIGURATION, "数据库必须使用 postgresql+psycopg")
    return url


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://agentgit:agentgit_local@127.0.0.1:55432/agentgit_dev"
    )
    test_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://agentgit:agentgit_local@127.0.0.1:55432/agentgit_test"
    )
    data_root: Path = Path(".agentgit/dev")
    test_data_root: Path = Path(".agentgit/test")
    max_object_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    connect_timeout_seconds: int = Field(default=3, ge=1, le=30)

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            return cls.model_validate(
                {
                    name: os.environ[f"AGENTGIT_{name.upper()}"]
                    for name in cls.model_fields
                    if f"AGENTGIT_{name.upper()}" in os.environ
                }
            )
        except ValidationError:
            raise AgentGitError(ErrorCode.CONFIGURATION, "环境配置字段无效") from None

    def assert_test_isolation(self) -> None:
        dev = self.data_root.resolve()
        test = self.test_data_root.resolve()
        if dev == test or dev in test.parents or test in dev.parents:
            raise AgentGitError(ErrorCode.CONFIGURATION, "开发和测试数据目录必须互不包含")
        dev_url = database_url(self.database_url)
        test_url = database_url(self.test_database_url)
        # 不以用户名或主机别名区分数据库；同名库一律保守拒绝。
        if (
            not test_url.database
            or dev_url.database == test_url.database
            or not test_url.database.endswith("_test")
        ):
            raise AgentGitError(ErrorCode.CONFIGURATION, "测试库须独立，且名称以 _test 结尾")
