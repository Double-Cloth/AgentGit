"""归档一个真实创建的报告与原始证据，供脱离临时测试库的审阅。"""

import base64
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from diagnosis_evidence import pack_repository_evidence
from test_debug_reports import failed_case as failed_case

from agentgit.core.debug_reports import DebugReport, NewDebugReport
from agentgit.core.models import parse_contract

pytestmark = pytest.mark.integration


def test_real_static_report_and_raw_evidence_are_consistent_offline(history, failed_case, tmp_path):
    manager, proof, _ = failed_case
    repo = UUID(proof["repository_id"])
    created = manager.create_debug_report(
        repo,
        NewDebugReport(
            failed_run_id=proof["failed_run_id"], reference_run_id=proof["reference_run_id"]
        ),
        "archive-static-demo",
    )
    stored = manager.debug_report(repo, UUID(created["debug_report_id"]))
    bundle = pack_repository_evidence(history, proof)
    raw = base64.b64decode(bundle["objects"][created["report_ref"]]["base64"], validate=True)
    report = parse_contract(raw, DebugReport)
    assert report.model_dump(mode="json") == stored["report"]
    assert (
        report.diagnosis.mode == "static_only" and report.diagnosis.intervention_improvement is None
    )
    assert report.failed.run_id == proof["failed_run_id"]
    destination = os.environ.get("AGENTGIT_DEBUG_REPORT_EVIDENCE_DIR")
    path = Path(destination).resolve() if destination else tmp_path / "archive"
    if destination:
        assert path.name.startswith("S8-debug-reports-") and path.is_relative_to(
            Path.cwd() / ".agentgit/evidence"
        )
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(
        json.dumps(stored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    (path / "evidence.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
