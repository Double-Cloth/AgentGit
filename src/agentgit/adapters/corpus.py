"""固定合成语料；不创建网络客户端，不请求资料 URL。"""

import hashlib
from datetime import datetime
from importlib.resources import files

from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.research import DEFAULT_FRAMEWORKS, EVALUATION_TIME, Source


def utc_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
        if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError
        return parsed
    except ValueError:
        raise AgentGitError(ErrorCode.VALIDATION, "语料时间必须是有效 UTC 时间") from None


class Corpus:
    def __init__(self, sources: tuple[Source, ...], *, evaluation_time: str = EVALUATION_TIME):
        self.evaluation_time = evaluation_time
        utc_time(evaluation_time)
        self.sources = {source.id: source for source in sources}
        if len(self.sources) != len(sources):
            raise AgentGitError(ErrorCode.VALIDATION, "语料 source ID 重复")
        for source in sources:
            if hashlib.sha256(source.body.encode("utf-8")).hexdigest() != source.body_sha256:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "语料正文摘要不符")
            if any(fact.quote not in source.body for fact in source.facts):
                raise AgentGitError(ErrorCode.VALIDATION, "语料引用不在正文中")
            if utc_time(source.valid_from) >= utc_time(source.valid_until):
                raise AgentGitError(ErrorCode.VALIDATION, "语料有效区间非法")
            utc_time(source.collected_at)
        if {source.framework for source in sources} != set(DEFAULT_FRAMEWORKS):
            raise AgentGitError(ErrorCode.VALIDATION, "语料必须覆盖约定的三个合成框架")

    @classmethod
    def bundled(cls) -> "Corpus":
        payload = files("agentgit.adapters").joinpath("fixtures/research.json").read_bytes()
        value = decode(payload)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, "未知语料版本")
        return cls(tuple(parse_contract(encode(item), Source) for item in value["sources"]))

    def search(self, frameworks: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            sorted(key for key, source in self.sources.items() if source.framework in frameworks)
        )

    def read(self, source_id: str) -> Source:
        if source_id not in self.sources:
            raise AgentGitError(ErrorCode.NOT_FOUND, "离线来源不存在")
        return self.sources[source_id]

    def digest(self) -> str:
        return hashlib.sha256(
            encode(
                {
                    "schema_version": 1,
                    "evaluation_time": self.evaluation_time,
                    "sources": [
                        self.sources[key].model_dump(mode="json") for key in sorted(self.sources)
                    ],
                }
            )
        ).hexdigest()
