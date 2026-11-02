"""真实 MCP 测试进程的就绪读取，覆盖 Windows 短暂共享访问错误。"""

from pathlib import Path

import pytest
import test_mcp_adapter as fixture_module

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("transient", [FileNotFoundError, PermissionError])
def test_ready_read_recovers_within_original_deadline(tmp_path, monkeypatch, transient):
    ready = tmp_path / "mcp-ready.json"
    original = Path.read_text
    injected = []

    def read(path, *args, **kwargs):
        # 仅在真实服务已原子发布标记后模拟一次拒绝，后续读取仍访问实际文件。
        if path == ready and path.exists() and not injected:
            injected.append(transient)
            raise transient("模拟发布后的短暂访问失败")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    fixture = fixture_module.server.__wrapped__(tmp_path)
    try:
        config, counts, _ = next(fixture)
        assert injected == [transient]
        assert config.endpoint.startswith("http://127.0.0.1:")
        assert counts()["requests"] == counts()["calls"] == 0
    finally:
        fixture.close()
