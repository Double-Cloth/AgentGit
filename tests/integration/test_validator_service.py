"""S7：真实 PostgreSQL 中的项目归属、历史快照和冻结验收结果。"""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenAssessment
from agentgit.core.models import parse_contract
from agentgit.core.research import ResearchGoal
from agentgit.services.research import ResearchService
from agentgit.services.validators import TABLES, ValidatorService

pytestmark = pytest.mark.integration


def completed_case(history, tmp_path):
    repo = history.create_repository("冻结验收")
    goal = ResearchGoal(text="比较固定语料中的三个框架")
    validators = ValidatorService(history)
    ref = validators.freeze(repo, goal, Corpus.bundled())
    research = ResearchService(history, tmp_path / "runs")
    config = research.create_config(repo)
    task = research.create_task(repo, config, goal)
    session = research.start(repo, task)
    session.execute()
    return repo, validators, ref, session


def test_real_research_is_independently_validated_and_deduplicated(history, tmp_path):
    repo, service, ref, session = completed_case(history, tmp_path)
    manifest = service.manifest(repo, ref)
    result = service.evaluate(repo, ref, session.run_id, session.head)
    assert result.verdict == "passed" and result.test_only
    assert service.evaluate(repo, ref, session.run_id, session.head) == result
    refs = TABLES["object_refs"]
    with history.engine.connect() as connection:
        validator_objects = (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repo,
                    refs.c.owner_type == "validator",
                    refs.c.owner_id == ref,
                )
            )
            .scalars()
            .all()
        )
        assessments = (
            connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repo,
                    refs.c.owner_type == "validator_assessment",
                )
            )
            .scalars()
            .all()
        )
    assert set(validator_objects) == {ref, manifest.input_ref, manifest.corpus_ref}
    assert len(assessments) == 1
    assert (
        parse_contract(history.objects.get(assessments[0], ObjectType.BLOB), FrozenAssessment)
        == result
    )
    assert service.manifest(repo, ref) == manifest


def test_cross_project_wrong_run_and_unowned_cas_are_inaccessible(history, tmp_path):
    repo, service, ref, session = completed_case(history, tmp_path)
    other = history.create_repository("其他项目")
    orphan = history.objects.put_json(ObjectType.BLOB, {"schema_version": 1}).id
    actions = (
        lambda: service.manifest(other, ref),
        lambda: service.manifest(repo, orphan),
        lambda: service.evaluate(repo, ref, uuid4(), session.head),
        lambda: service.evaluate(other, ref, session.run_id, session.head),
    )
    for action in actions:
        with pytest.raises(AgentGitError) as caught:
            action()
        assert caught.value.code == ErrorCode.NOT_FOUND


def test_wrong_frozen_goal_cannot_validate_real_run(history, tmp_path):
    repo, service, _, session = completed_case(history, tmp_path)
    wrong = service.freeze(repo, ResearchGoal(text="其他任务"), Corpus.bundled())
    with pytest.raises(AgentGitError) as caught:
        service.evaluate(repo, wrong, session.run_id, session.head)
    assert caught.value.code == ErrorCode.REPLAY_MISMATCH
    with history.engine.connect() as connection:
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(TABLES["object_refs"])
                .where(TABLES["object_refs"].c.owner_type == "validator_assessment")
            ).scalar_one()
            == 0
        )


def test_intermediate_checkpoint_is_incomplete(history, tmp_path):
    repo, service, ref, session = completed_case(history, tmp_path)
    links = TABLES["run_commits"]
    with history.engine.connect() as connection:
        first = connection.execute(
            sa.select(links.c.commit_id)
            .where(links.c.run_id == session.run_id)
            .order_by(links.c.run_order)
            .limit(1)
        ).scalar_one()
    result = service.evaluate(repo, ref, session.run_id, first)
    assert result.verdict == "incomplete"
    assert not result.evaluation.passed
