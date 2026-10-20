"""本地效果分类与 fixture 端点的边界。"""

import pytest
from pydantic import ValidationError

from agentgit.adapters.fake_effect import FakeEffectAdapter
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import EffectClass


@pytest.mark.parametrize(
    "effect,scope",
    [
        (EffectClass.IDEMPOTENT_WRITE, "external"),
        (EffectClass.NON_IDEMPOTENT_WRITE, "external"),
        (EffectClass.IRREVERSIBLE, "external"),
        (EffectClass.IRREVERSIBLE, "sandbox"),
        (EffectClass.NON_IDEMPOTENT_WRITE, "local"),
    ],
)
def test_external_writes_and_irreversible_actions_cannot_be_preauthorized(effect, scope):
    with pytest.raises(ValidationError):
        ToolRule(
            provider="tool:fixture/v1",
            signature_digest="a" * 64,
            effect=effect,
            scope=scope,
            preauthorized=True,
        )


def test_sandbox_preauthorization_is_distinct_from_external_write():
    rule = ToolRule(
        provider="tool:terminal_exec/v1",
        signature_digest="a" * 64,
        effect=EffectClass.NON_IDEMPOTENT_WRITE,
        scope="sandbox",
        preauthorized=True,
    )
    assert ToolPolicy(rules=(rule,)).rules == (rule,)
    with pytest.raises(ValidationError):
        ToolPolicy(rules=(rule, rule))


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:1234",
        "http://example.com:1234",
        "http://localhost:1234",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:1234",
        "http://127.0.0.1:1234/effect",
        "http://127.0.0.1:1234?target=other",
        "http://127.0.0.1:bad",
    ],
)
def test_fixture_endpoint_cannot_be_retargeted(url):
    with pytest.raises(AgentGitError) as caught:
        FakeEffectAdapter(url)
    assert caught.value.code == ErrorCode.CONFIGURATION
