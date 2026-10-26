"""单连接只读解析实际派生补丁，项目和冻结历史约束不能绕过。"""

from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_repair_field_boundaries import begin_field
from test_repair_worker import ConditionalMock

from agentgit.adapters.tools import ToolRegistry
from agentgit.core.canonical import encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract
from agentgit.core.repair_validation import RepairCandidate
from agentgit.services.history import HistoryService
from agentgit.services.jobs import JobService
from agentgit.services.repair_field_trials import RepairFieldTrialService
from agentgit.services.repair_fields import TABLES, RepairFieldService
from agentgit.services.repair_studies import RepairStudyService
from agentgit.worker import Worker

pytestmark = pytest.mark.integration


def test_field_read_and_derived_projection_use_single_connection_and_reject_wrong_project(
    history, tmp_path, monkeypatch
):
    repo, study, research, ledger, fields, lease, identity, _ = begin_field(history, tmp_path)
    trials = RepairFieldTrialService(research)
    trials.dispatch(lease, identity)
    Worker(JobService(research), model_factory=ConditionalMock).run_once()
    assert trials.settle(lease, identity)["result"]["execution"]["verdict"] == "passed"
    before = ledger.view(repo, study)
    with history.engine.connect() as connection:
        schema = connection.execute(sa.text("SELECT current_schema()")).scalar_one()
        source = fields.source(connection, repo, study)
    assert schema.startswith("test_") and schema[5:].isalnum()
    engine = sa.create_engine(
        history.engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.5,
        connect_args={"options": f"-csearch_path={schema}", "connect_timeout": 3},
        hide_parameters=True,
    )
    scoped = HistoryService(engine, history.objects)

    def forbidden(*args, **kwargs):
        pytest.fail("派生查询和投影不能执行模型、工具或写 CAS")

    try:
        with monkeypatch.context() as reading:
            reading.setattr(history.objects, "put", forbidden)
            reading.setattr(ConditionalMock, "invoke", forbidden)
            reading.setattr(ToolRegistry, "invoke", forbidden)
            service, field_service = RepairStudyService(scoped), RepairFieldService(scoped)
            fetched = service.view(repo, study)
            assert fetched["field_reduction"] == before["field_reduction"]
            candidate = before["field_reduction"]["summary"]["final_candidate"]
            reference = candidate["atoms"][0]["patch_ref"]
            with engine.connect() as connection:
                patch, _ = field_service.resolve_patch(connection, repo, study, source, reference)
                with pytest.raises(AgentGitError) as error:
                    field_service.resolve_patch(connection, uuid4(), study, source, reference)
                assert error.value.code == ErrorCode.FORBIDDEN
            applied = service._applied(
                repo, source, parse_contract(encode(candidate), RepairCandidate), study_id=study
            )
            assert applied.config.prompt == patch.intervention.payload.prompt
        with pytest.raises(DBAPIError), history.engine.begin() as connection:
            connection.execute(
                TABLES["repair_field_patches"]
                .update()
                .where(TABLES["repair_field_patches"].c.study_id == study)
                .values(parent_patch_ref="f" * 64)
            )
        with (
            pytest.raises(DBAPIError, match="original atom phase is frozen"),
            history.engine.begin() as connection,
        ):
            old = dict(
                connection.execute(sa.select(TABLES["repair_reduction_trials"])).mappings().one()
            )
            old.update(
                id=uuid4(),
                ordinal=1,
                intent_key="forged-field-bypass",
                protocol_ref=before["state_ref"],
                branch_name=f"reduce-{study.hex}-1",
            )
            connection.execute(TABLES["repair_reduction_trials"].insert().values(**old))
    finally:
        engine.dispose()
