"""项目限定的冻结验收用例；输入和结果均保留不可变 CAS 引用。"""

from typing import Literal
from uuid import UUID

import sqlalchemy as sa

from agentgit.adapters.corpus import Corpus
from agentgit.core.canonical import ObjectType, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.experiments import FrozenAssessment, ValidatorManifest
from agentgit.core.models import parse_contract
from agentgit.core.research import AgentState, ResearchGoal
from agentgit.experiments.validation import evaluate_frozen, freeze_validator
from agentgit.services.history import HistoryService
from agentgit.storage.schema import build_metadata

TABLES = build_metadata().tables


class ValidatorService:
    def __init__(self, history: HistoryService):
        self.history = history

    def freeze(
        self,
        repository_id: UUID,
        goal: ResearchGoal,
        corpus: Corpus,
        *,
        fixture_split: Literal["screen", "confirm", "regression"] = "screen",
    ) -> str:
        with self.history.engine.connect() as connection:
            if (
                connection.execute(
                    sa.select(TABLES["repositories"].c.id).where(
                        TABLES["repositories"].c.id == repository_id
                    )
                ).first()
                is None
            ):
                raise AgentGitError(ErrorCode.NOT_FOUND, "项目不可访问")
        manifest = freeze_validator(self.history.objects, goal, corpus, fixture_split=fixture_split)
        item = self.history.objects.put_json(ObjectType.BLOB, manifest.model_dump(mode="json"))
        references = [("manifest", item)]
        for role, ref in (("input", manifest.input_ref), ("corpus", manifest.corpus_ref)):
            references.append(
                (
                    role,
                    self.history.objects.put(
                        ObjectType.BLOB,
                        self.history.objects.get(ref, ObjectType.BLOB),
                        media_type="application/json",
                    ),
                )
            )
        with self.history.engine.begin() as connection:
            for role, obj in references:
                self.history._ref(connection, repository_id, obj, "validator", item.id, role)
        return item.id

    def manifest(self, repository_id: UUID, validator_ref: str) -> ValidatorManifest:
        refs = TABLES["object_refs"]
        with self.history.engine.connect() as connection:
            owned = connection.execute(
                sa.select(refs.c.object_id).where(
                    refs.c.repository_id == repository_id,
                    refs.c.object_id == validator_ref,
                    refs.c.owner_type == "validator",
                    refs.c.owner_id == validator_ref,
                    refs.c.role == "manifest",
                )
            ).first()
        if owned is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "冻结验收器不可访问")
        return parse_contract(
            self.history.objects.get(validator_ref, ObjectType.BLOB), ValidatorManifest
        )

    def evaluate(
        self,
        repository_id: UUID,
        validator_ref: str,
        run_id: UUID,
        commit_id: str,
    ) -> FrozenAssessment:
        manifest = self.manifest(repository_id, validator_ref)
        links, commits = TABLES["run_commits"], TABLES["commits"]
        with self.history.engine.connect() as connection:
            state_ref = connection.execute(
                sa.select(commits.c.state_ref)
                .select_from(
                    links.join(
                        commits,
                        sa.and_(
                            commits.c.repository_id == links.c.repository_id,
                            commits.c.id == links.c.commit_id,
                        ),
                    )
                )
                .where(
                    links.c.repository_id == repository_id,
                    links.c.run_id == run_id,
                    links.c.commit_id == commit_id,
                )
            ).scalar_one_or_none()
        if state_ref is None:
            raise AgentGitError(ErrorCode.NOT_FOUND, "验收检查点不属于指定项目和运行")
        snapshot = self.history.restore(repository_id, commit_id)
        state = parse_contract(encode(snapshot.data), AgentState)
        contents = {
            item.path: self.history.objects.get(item.object_ref, ObjectType.BLOB)
            for item in snapshot.artifacts
        }
        verdict, evaluation = evaluate_frozen(self.history.objects, manifest, state, contents)
        result = FrozenAssessment(
            validator_ref=validator_ref,
            run_id=str(run_id),
            commit_id=commit_id,
            state_ref=state_ref,
            verdict=verdict,
            evaluation=evaluation,
        )
        obj = self.history.objects.put_json(ObjectType.BLOB, result.model_dump(mode="json"))
        with self.history.engine.begin() as connection:
            self.history._ref(
                connection, repository_id, obj, "validator_assessment", obj.id, "assessment"
            )
        return result
