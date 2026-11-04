"""有覆盖范围的精确实验统计；不以未完成样本、未知值或混合单位填补成功。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any


def ratio(numerator: int, denominator: int) -> dict[str, str] | None:
    if denominator == 0:
        return None
    value = Fraction(numerator, denominator)
    # 分子和平方和可能超过 AGJSON 安全整数；十进制字符串保留精确值。
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def describe(values: Sequence[int | None], *, population: int, unit: str) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    if any(type(value) is not int or value < 0 for value in known) or population < len(values):
        raise ValueError("统计需要非负整数观测及完整的样本范围")
    count, total = len(known), sum(known)
    return {
        "unit": unit,
        "population": population,
        "samples": count,
        "missing_samples": len(values) - count,
        "not_applicable_samples": population - len(values),
        "sum": str(total) if count == len(values) and count else None,
        "known_sum": str(total) if count else None,
        "min": str(min(known)) if count else None,
        "max": str(max(known)) if count else None,
        "mean": ratio(total, count),
        "sample_variance": ratio(
            count * sum(value * value for value in known) - total * total, count * (count - 1)
        )
        if count > 1
        else None,
    }


def _scope(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: dict[str, tuple[str, list[int | None]]] = {}

    def add(name: str, unit: str, value: int | None) -> None:
        values.setdefault(name, (unit, []))[1].append(value)

    for item in items:
        replay = item["replay"]
        actual, logical = replay["actual"], replay["new_usage"]
        for name in (
            "model_calls",
            "tool_calls",
            "unknown_outcomes",
            "fixture_calls",
            "recorded_observations",
            "synthetic_observations",
            "hybrid_misses",
        ):
            add("actual." + name, "count", actual.get(name, 0))
        add("actual.wall_ms", "ms", actual["wall_ms"])
        measurement = actual["model_usage"]
        add("mock.total_bytes", "mock_utf8_bytes", measurement["mock"]["total_bytes"])
        provider = measurement["provider"]
        for name in (
            "input_tokens",
            "output_tokens",
            "accounted_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ):
            add("provider." + name, "provider_tokens", provider[name])
        for name in (
            "estimated_cost_micro_usd",
            "known_estimated_cost_micro_usd",
            "reserved_cost_micro_usd",
            "billed_cost_micro_usd",
        ):
            add("provider." + name, "micro_USD", provider[name])
        for name in (
            "measured_calls",
            "estimated_usage_calls",
            "not_generated_calls",
            "missing_meter_calls",
            "unpriced_calls",
        ):
            add("provider." + name, "count", provider[name])
        kind = replay["logical_usage"]["usage_kind"]
        add("checkpoint.tokens." + kind, kind, logical["tokens"])
        for name in ("model_calls", "tool_calls", "steps", "replans"):
            add("checkpoint." + name, "count", logical[name])
    return {
        "trial_count": len(items),
        "metrics": {
            name: describe(samples, population=len(items), unit=unit)
            for name, (unit, samples) in sorted(values.items())
        },
    }


def experiment_statistics(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups = []
    for key in ("A", "B"):
        trials = [item for item in items if item["group"] == key]
        assessed = [item for item in trials if item["verdict"] in ("passed", "failed")]
        passed = sum(item["verdict"] == "passed" for item in trials)
        failed = len(assessed) - passed
        recovered = [item for item in trials if item["attempt"] > 1]
        recovered_passed = sum(item["verdict"] == "passed" for item in recovered)
        errors = Counter(
            error for item in assessed for error in set(item["assessment"]["evaluation"]["errors"])
        )
        groups.append(
            {
                "key": key,
                "planned_trials": len(trials),
                "passed": passed,
                "failed": failed,
                "incomplete": len(trials) - len(assessed),
                "success_rate": ratio(passed, len(trials)),
                "assessed_success_rate": ratio(passed, len(assessed)),
                "statuses": dict(sorted(Counter(item["status"] for item in trials).items())),
                "hard_assertions": {
                    "scope": "whole_frozen_validator",
                    "passed": passed,
                    "failed": failed,
                    "not_evaluated": len(trials) - len(assessed),
                    "assessed_pass_rate": ratio(passed, len(assessed)),
                    "failure_codes": dict(sorted(errors.items())),
                },
                "recovery": {
                    "trials": len(recovered),
                    "passed": recovered_passed,
                    "success_rate": ratio(recovered_passed, len(recovered)),
                },
                "consumption_to_date": _scope(trials),
                "assessed_trials": _scope(assessed),
            }
        )
    return {
        "schema_version": 1,
        "kind": "experiment_statistics/v1",
        "snapshot": "per_trial_bounded_projection",
        "groups": groups,
        "limitations": [
            "成功率包含所有预定 trial；完整执行分布仅含已独立验收的成功或失败样本。",
            "均值和样本方差仅基于已知观测，缺失数量单列；单样本没有样本方差。",
            "Mock 字节、供应商 Token、检查点逻辑用量和实际调用账本分别统计。",
            "供应商费用为固定价格表估计，未知或未核验费用保留预留；未取得供应商账单。",
            "细分验收规则可能短路，错误列表中未出现某规则不代表该规则已通过。",
            "固定 fixture/seed 的重复结果不证明真实模型波动、泛化稳定性或严格因果。",
        ],
    }
