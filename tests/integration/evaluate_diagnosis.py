"""冻结后运行完整离线评估；失败样例不漏行，不把提案标签当人工 gold。"""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from diagnosis_dataset import load_dataset
from diagnosis_evaluation import aggregate, analyze_evidence, score_targets

from agentgit.core.canonical import encode
from agentgit.core.diagnosis import TraceGraph
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.services.environment import source_revision

ROOT = Path(__file__).resolve().parents[2]
MODES = ("with_reference", "without_reference")


def bounded(path):
    path = path.resolve()
    if not path.is_relative_to(ROOT) or path.stat().st_size > 32 * 1024 * 1024:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "评估输入路径或字节数超出范围")
    return path.read_bytes()


def checked_json(path, digest):
    raw = bounded(path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "评估文件与冻结摘要不同")
    return json.loads(raw)


def validate_release(path):
    release = json.loads(bounded(path))
    if (
        release.get("schema_version") != 1
        or release.get("status") != "frozen_before_heldout_evaluation"
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "必须先冻结评估脚本与指标定义")
    for name, digest in release["implementation_sha256"].items():
        if hashlib.sha256(bounded(ROOT / name)).hexdigest() != digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "评估实现已变化，不能复用冻结评估版本")
    rules = checked_json(ROOT / release["rules_file"], release["rules_file_sha256"])
    for name, digest in rules["implementation_sha256"].items():
        if hashlib.sha256(bounded(ROOT / name)).hexdigest() != digest:
            raise AgentGitError(
                ErrorCode.REPLAY_MISMATCH, "排序或证据提取实现已变化，不能复用已见保留集"
            )
    if source_revision() != rules["source_revision"]:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "评估产品源码与冻结上下文不同")
    dataset = load_dataset()
    for name, digest in (
        ("tests/fixtures/diagnosis/v1/cases.json", rules["dataset"]["cases_sha256"]),
        ("tests/integration/diagnosis_dataset.py", rules["dataset"]["fixture_sha256"]),
    ):
        if hashlib.sha256(bounded(ROOT / name)).hexdigest() != digest:
            raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "当前样例与冻结排序上下文不同")
    if rules["dataset"]["train"] != [case.id for case in dataset.cases[:12]] or rules["dataset"][
        "heldout"
    ] != [case.id for case in dataset.cases[12:]]:
        raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "冻结评估划分不同")
    manifest = checked_json(ROOT / release["input_summary_file"], release["input_summary_sha256"])
    if {row["id"] for row in manifest["cases"]} != {case.id for case in dataset.cases}:
        raise AgentGitError(ErrorCode.VALIDATION, "冻结证据必须包含全部 20 个样例")
    return release, rules, dataset, manifest


def evaluate(release_file, evidence_dir):
    release, rules, dataset, manifest = validate_release(release_file)
    indexes = {row["id"]: row for row in manifest["cases"]}
    rows = []
    for case in dataset.cases:
        row = {
            "id": case.id,
            "split": case.split,
            "annotation_origin": case.annotation.origin,
            "annotation_status": case.annotation.review_status,
        }
        try:
            entry = indexes[case.id]
            if entry["split"] != case.split:
                raise AgentGitError(ErrorCode.REPLAY_MISMATCH, "证据划分与冻结规格不同")
            proof = checked_json(evidence_dir / f"{case.id}.json", entry["proof_sha256"])
            bundle = checked_json(evidence_dir / f"{case.id}-evidence.json", entry["bundle_sha256"])
            if (
                proof["case_id"] != case.id
                or proof["split"] != case.split
                or proof["annotation_review_status"] != "pending_human"
                or proof["fault_fixture_sha256"] != rules["dataset"]["cases_sha256"]
            ):
                raise AgentGitError(ErrorCode.VALIDATION, "证据样例或提案状态不同")
            failed = parse_contract(encode(proof["graph"]), TraceGraph)
            reference = parse_contract(encode(proof["reference_graph"]), TraceGraph)
            for mode in MODES:
                try:
                    analysis = analyze_evidence(
                        failed, reference if mode == "with_reference" else None, bundle
                    )
                    scores = score_targets(analysis, proof["resolved_labels"], failed)
                    row[mode] = {"status": "analyzed", "analysis": analysis, "scores": scores}
                except Exception as error:
                    # 保留所有失败并使进程最终非零；不因单例异常丢掉整份评估。
                    row[mode] = {
                        "status": "analysis_error",
                        "error_type": type(error).__name__,
                        "error_code": error.code.value
                        if isinstance(error, AgentGitError)
                        else None,
                    }
        except Exception as error:
            for mode in MODES:
                row[mode] = {
                    "status": "evidence_error",
                    "error_type": type(error).__name__,
                    "error_code": error.code.value if isinstance(error, AgentGitError) else None,
                }
        rows.append(row)
    groups = {
        mode: {
            split: aggregate([row for row in rows if split == "all" or row["split"] == split], mode)
            for split in ("all", "train", "heldout")
        }
        for mode in MODES
    }
    primary = groups["with_reference"]["heldout"]["targets"]["injection_point"]
    numeric_met = primary["top3_hit"] >= 6 and primary["top3_hit"] >= primary["baseline_hit"]
    return {
        "schema_version": 1,
        "kind": "provisional_diagnosis_evaluation/v1",
        "evaluated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "annotation_status": "pending_human",
        "formal_s8_t04_passed": False,
        "formal_s8_t05_passed": False,
        "rules_digest": rules["rules_digest"],
        "source_revision": source_revision(),
        "evaluation_release_sha256": hashlib.sha256(bounded(release_file)).hexdigest(),
        "input_summary_sha256": release["input_summary_sha256"],
        "case_evidence_source_revision": manifest["source_revision"],
        "case_count": len(rows),
        "rows": rows,
        "groups": groups,
        "heldout_primary_target": {
            "metric": "injection_point_exact_event_top3",
            "minimum": 6,
            "denominator": 8,
            "hit": primary["top3_hit"],
            "time_baseline_hit": primary["baseline_hit"],
            "numeric_target_met": numeric_met,
            "formal_verdict": "unverified_pending_human",
        },
        "analysis_cost": {
            "model_calls": 0,
            "tool_calls": 0,
            "provider_calls": 0,
            "currency": "USD",
            "provider_billed_cost": None,
            "model_call_cost": 0,
            "cpu_io_cost": None,
        },
        "case_construction_cost": manifest["case_construction_cost"],
        "limitations": [
            "全部标签是代理提案，未经人工审阅；暂定工程结果不能算作 S8-T04/T05 正式通过。",
            "主要目标是注入点精确事件 Top-k；不能用拟修复决策命中替代主要指标。"
            "冻结输入不是模型决策事件的样例仍保留在分母。",
            "时间基线是首个显式错误或失败验收事件，按 recorded_at、同刻 seq 寻址。"
            "不将 gold 注入点当基线预测。",
            "只读分析历史证据，没有重新执行原任务或运行干预；源运行构造消耗与定位开销分开。",
            "定位步骤限定为距离搜索出队节点数，并列跨 Top-k 边界按未确定命中处理。"
            "原始候选和失败全部保留。",
            "有/无参考两组来自同一 20 例，不是 40 个独立样本。",
        ],
    }


def render_markdown(report):
    lines = [
        "# S8 暂定离线定位评估",
        "",
        "全部标注仍为代理提案，未经人工审阅；以下不是 S8-T04/T05 的正式通过结论。",
        "",
        "主要指标是注入点精确事件 Top-k，拟修复决策的命中另列，不能替代主要目标。",
        "",
        "| 模式 / 划分 | 分母 | 注入 Top-1 | 注入 Top-3 | 时间基线 | 未定位 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        for split, group in report["groups"][mode].items():
            target = group["targets"]["injection_point"]
            lines.append(
                f"| {mode} / {split} | {group['scheduled']} | {target['top1_hit']} "
                f"| {target['top3_hit']} | {target['baseline_hit']} | {group['unlocated']} |"
            )
    lines.extend(
        [
            "",
            "| 模式 / 划分 | 拟修复决策 Top-1 | 拟修复决策 Top-3 | 首个异常观测覆盖 |",
            "|---|---:|---:|---:|",
        ]
    )
    for mode in MODES:
        for split, group in report["groups"][mode].items():
            target = group["targets"]["repairable_decision"]
            lines.append(
                f"| {mode} / {split} | {target['top1_hit']} | {target['top3_hit']} "
                f"| {group['first_visible_observation_coverage']['covered']} |"
            )
    target = report["heldout_primary_target"]
    lines.extend(
        [
            "",
            f"保留集主要 Top-3：{target['hit']}/8；目标至少 6/8，"
            f"数值达标：{target['numeric_target_met']}；正式状态仍待人工标注审阅。",
            "",
            "逐例预测、并列、失败、精确均值、距离遍历步骤、实际耗时及构造/定位开销"
            "详见同目录 evaluation.json。",
            "",
            *[f"- {text}" for text in report["limitations"]],
            "",
        ]
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="冻结后的 S8 离线评估")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    target = args.output_dir.resolve()
    if (
        not target.is_relative_to(ROOT / ".agentgit/evidence")
        or not target.name.startswith("S8-evaluation-")
        or target.exists()
    ):
        raise AgentGitError(ErrorCode.VALIDATION, "输出必须是新的工作区 S8-evaluation-* 证据目录")
    report = evaluate(args.release, args.evidence_dir)
    target.mkdir(parents=True)
    (target / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (target / "evaluation.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                "case_count": report["case_count"],
                "heldout_primary_target": report["heldout_primary_target"],
                "formal_s8_t05_passed": False,
            },
            ensure_ascii=False,
        )
    )
    return (
        1
        if any(row[mode]["status"] != "analyzed" for row in report["rows"] for mode in MODES)
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
