"""在工具与模型之外执行的固定研究报告验收器。"""

from collections.abc import Mapping

from agentgit.adapters.corpus import Corpus, utc_time
from agentgit.core.errors import AgentGitError
from agentgit.core.models import Contract, parse_contract
from agentgit.core.research import AgentState, Evaluation, Evidence

DIMENSIONS = ("调度方式", "状态存储", "适用规模")


class ReportClaim(Contract):
    framework: str
    dimension: str
    value: str
    source_id: str
    quote: str


class ResearchReport(Contract):
    fixture: bool = True
    evaluation_time: str
    frameworks: tuple[str, ...]
    claims: tuple[ReportClaim, ...]
    sources: tuple[Evidence, ...]


def report_from_memory(state: AgentState, evaluation_time: str) -> ResearchReport:
    sources = tuple(state.working_memory[key] for key in sorted(state.working_memory))
    return ResearchReport(
        evaluation_time=evaluation_time,
        frameworks=state.goal.frameworks,
        sources=sources,
        claims=tuple(
            ReportClaim(
                framework=item.source.framework,
                dimension=fact.dimension,
                value=fact.value,
                source_id=item.source.id,
                quote=fact.quote,
            )
            for item in sources
            for fact in item.source.facts
        ),
    )


def render_report(report: ResearchReport) -> str:
    lines = [
        "# 合成框架对比报告",
        "",
        "资料类型：固定合成 fixture；不代表真实产品或实时互联网事实。",
        f"评估时间：{report.evaluation_time}",
        "",
    ]
    for framework in report.frameworks:
        lines.extend([f"## {framework}", ""])
        for claim in report.claims:
            if claim.framework == framework:
                lines.append(f"- {claim.dimension}：{claim.value} [{claim.source_id}]")
        lines.append("")
    return "\n".join(lines)


def validate_report(state: AgentState, contents: Mapping[str, bytes], corpus: Corpus) -> Evaluation:
    errors: list[str] = []
    if "sources.json" not in contents or "report.md" not in contents:
        return Evaluation(passed=False, errors=("missing_artifacts",))
    try:
        report = parse_contract(contents["sources.json"], ResearchReport)
        if not report.fixture or report.evaluation_time != corpus.evaluation_time:
            errors.append("fixture_or_evaluation_time")
        if report.frameworks != state.goal.frameworks:
            errors.append("framework_coverage")
        expected_ids = set(corpus.search(state.goal.frameworks))
        evidence = {item.source.id: item for item in report.sources}
        if set(evidence) != expected_ids or len(evidence) != len(report.sources):
            errors.append("source_coverage")
        for source_id, item in evidence.items():
            expected = corpus.read(source_id)
            if item.source != expected or state.working_memory.get(source_id) != item:
                errors.append(f"evidence_mismatch:{source_id}")
            if not (
                utc_time(expected.valid_from)
                <= utc_time(corpus.evaluation_time)
                < utc_time(expected.valid_until)
            ):
                errors.append(f"source_expired:{source_id}")
            if utc_time(expected.collected_at) > utc_time(corpus.evaluation_time):
                errors.append(f"source_future:{source_id}")
        pairs: set[tuple[str, str]] = set()
        for claim in report.claims:
            key = (claim.framework, claim.dimension)
            if key in pairs:
                errors.append("duplicate_claim")
            pairs.add(key)
            source = corpus.read(claim.source_id)
            if (
                claim.source_id not in evidence
                or source.framework != claim.framework
                or not any(
                    (fact.dimension, fact.value, fact.quote)
                    == (claim.dimension, claim.value, claim.quote)
                    for fact in source.facts
                )
                or claim.quote not in source.body
            ):
                errors.append(f"claim_without_evidence:{claim.source_id}")
        if pairs != {
            (framework, dimension)
            for framework in state.goal.frameworks
            for dimension in DIMENSIONS
        }:
            errors.append("claim_coverage")
        if contents["report.md"] != render_report(report).encode("utf-8"):
            errors.append("report_structure_or_claim_mismatch")
    except (AgentGitError, UnicodeError, ValueError):
        errors.append("report_schema")
    return Evaluation(passed=not errors, errors=tuple(errors))
