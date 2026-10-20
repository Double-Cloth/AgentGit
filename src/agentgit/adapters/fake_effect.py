"""仅测试用的显式回环服务 Adapter；不接入离线研究 Demo。"""

from http.client import HTTPException
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import Field

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, Digest, Identifier, parse_contract
from agentgit.runtime.ports import VerifiedOutcome


class FakeEffectRequest(Contract):
    operation_id: Identifier
    value: Annotated[str, Field(max_length=200)]
    response_mode: Literal["reply", "drop"] = "reply"


class FakeEffectReceipt(Contract):
    operation_id: Identifier
    request_digest: Digest
    value: Annotated[str, Field(max_length=200)]
    applied: Literal[True] = True


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class FakeEffectAdapter:
    provider = "tool:fake_effect/v1"

    def __init__(self, base_url: str):
        try:
            parts = urlsplit(base_url)
            port = parts.port
        except ValueError:
            raise AgentGitError(ErrorCode.CONFIGURATION, "fake effect 端点格式无效") from None
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or port is None
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise AgentGitError(ErrorCode.CONFIGURATION, "fake effect 只能使用显式登记的回环端点")
        self.base_url = base_url
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.signature = object_digest(
            ObjectType.CALL,
            encode(
                {
                    "schema_version": 1,
                    "provider": self.provider,
                    "endpoint": base_url,
                    "arguments": FakeEffectRequest.model_json_schema(),
                    "receipt": FakeEffectReceipt.model_json_schema(),
                    "fixture_only": True,
                }
            ),
        )

    def invoke(self, request: bytes) -> bytes:
        parse_contract(request, FakeEffectRequest)
        try:
            with self.opener.open(
                Request(
                    self.base_url + "/effect",
                    request,
                    {"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=2,
            ) as response:
                payload = bytes(response.read(4097))
            if len(payload) > 4096:
                raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "效果回执超过上限，需要核对")
            self._validate(request, payload)
            return payload
        except (OSError, URLError, ValueError):
            raise AgentGitError(
                ErrorCode.OUTCOME_UNKNOWN, "效果请求已发送但未取得完整回执"
            ) from None

    def lookup(self, request: bytes) -> bytes | None:
        action = parse_contract(request, FakeEffectRequest)
        try:
            with self.opener.open(
                self.base_url + "/effects/" + action.operation_id, timeout=2
            ) as response:
                payload = bytes(response.read(4097))
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "效果查询没有取得可信结果") from None
        except (OSError, URLError, HTTPException):
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "效果查询不可用") from None
        if len(payload) > 4096:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "效果查询回执超出上限")
        self._validate(request, payload)
        return payload

    def resolve(self, request: bytes) -> VerifiedOutcome | None:
        receipt = self.lookup(request)
        return VerifiedOutcome(receipt, receipt) if receipt is not None else None

    @staticmethod
    def _validate(request: bytes, response: bytes) -> None:
        action = parse_contract(request, FakeEffectRequest)
        receipt = parse_contract(response, FakeEffectReceipt)
        if (
            receipt.operation_id != action.operation_id
            or receipt.value != action.value
            or receipt.request_digest != object_digest(ObjectType.BLOB, request)
        ):
            raise AgentGitError(ErrorCode.OUTCOME_UNKNOWN, "效果回执与原始动作不符")
