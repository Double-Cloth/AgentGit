"""将一次搜索观察绑定到独立 fake 写服务，仅用于 S4 集成测试。"""

import hashlib
import json
import re
import sys
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import create_engine

from agentgit.adapters.fake_effect import FakeEffectAdapter, FakeEffectRequest
from agentgit.adapters.mock import MockResearchModel
from agentgit.adapters.tools import SearchArguments
from agentgit.config import Settings, database_url
from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.effects import ToolPolicy, ToolRule
from agentgit.core.models import EffectClass, parse_contract
from agentgit.runtime.ports import VerifiedOutcome
from agentgit.services.approvals import ApprovalService
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.research import ResearchService
from agentgit.storage.objects import ObjectStore
from agentgit.worker import Worker


class EffectfulSearchResolver:
    provider = "tool:corpus_search/v1"

    def __init__(self, adapter, namespace, corpus):
        self.adapter, self.namespace, self.corpus = adapter, namespace, corpus
        self.lookups = 0
        self.signature = object_digest(
            ObjectType.CALL,
            encode(
                {
                    "schema_version": 1,
                    "fixture": "effectful_search/v1",
                    "adapter": adapter.signature,
                    "namespace": str(namespace),
                    "corpus": corpus.digest(),
                    "fixture_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                }
            ),
        )

    def external_request(self, request):
        digest = object_digest(ObjectType.BLOB, request)
        return encode(
            FakeEffectRequest(
                operation_id=str(uuid5(self.namespace, digest)), value=digest, response_mode="drop"
            ).model_dump(mode="json")
        )

    def search_response(self, request):
        parsed = parse_contract(request, SearchArguments)
        return encode(
            {"schema_version": 1, "source_ids": list(self.corpus.search(parsed.frameworks))}
        )

    def invoke(self, request):
        self.adapter.invoke(self.external_request(request))
        return self.search_response(request)

    def resolve(self, request):
        self.lookups += 1
        verified = self.adapter.resolve(self.external_request(request))
        if verified is None:
            return None
        return VerifiedOutcome(self.search_response(request), verified.evidence)


class EffectfulWorker(Worker):
    def __init__(self, queue, base_url, namespace, **kwargs):
        super().__init__(queue, **kwargs)
        self.resolver = EffectfulSearchResolver(
            FakeEffectAdapter(base_url), namespace, queue.research.corpus
        )

    def _attach(self, lease):
        session, origins, remaining = super()._attach(lease)
        effect, signature, invoke = (
            session.tools.effect,
            session.tools.signature,
            session.tools.invoke,
        )
        resolver = self.resolver
        session.tools.effect = lambda name: (
            EffectClass.NON_IDEMPOTENT_WRITE if name == "corpus_search" else effect(name)
        )
        session.tools.signature = lambda name: (
            resolver.signature if name == "corpus_search" else signature(name)
        )
        session.tools.invoke = lambda name, arguments: (
            resolver.invoke(encode(arguments))
            if name == "corpus_search"
            else invoke(name, arguments)
        )
        rules = [
            ToolRule(
                provider="llm:mock-research/v1",
                signature_digest=MockResearchModel.signature,
                effect=EffectClass.READ_ONLY,
                scope="local",
                preauthorized=True,
            )
        ]
        rules.extend(
            ToolRule(
                provider=f"tool:{name}/v1",
                signature_digest=session.tools.signature(name),
                effect=session.tools.effect(name),
                scope="external" if name == "corpus_search" else "local",
                preauthorized=name != "corpus_search",
            )
            for name in sorted(session.config.tools)
        )
        session.authorization = ApprovalService(
            session.history, ToolPolicy(rules=tuple(rules)), actor="fixture-operator"
        )
        return session, origins, remaining


def main():
    schema, data, objects, url, namespace, point = sys.argv[1:]
    assert re.fullmatch("test_[0-9a-f]{32}", schema)
    settings = Settings.from_env()
    settings.assert_test_isolation()
    engine = create_engine(
        database_url(settings.test_database_url),
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    try:
        history = HistoryService(engine, ObjectStore(Path(objects)))
        queue = JobService(ResearchService(history, Path(data)), lease_ms=1500)

        count = 0

        def fault(current):
            nonlocal count
            if current == "after_result":
                count += 1
            if current == point or (
                point == "after_unknown_result" and current == "after_result" and count == 1
            ):
                import os

                os._exit(73)

        result = EffectfulWorker(queue, url, UUID(namespace), fault=fault).run_once()
        print(json.dumps(result))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
