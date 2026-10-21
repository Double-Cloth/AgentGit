"""安全 Fork、完整来源路径、只读浏览与无共同祖先的真实数据库验证。"""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, CallOccurrence, EffectClass, EnvironmentManifest
from agentgit.core.versions import NewBranch, RecordedRange
from agentgit.services.history import TABLES, ArtifactInput, EventInput
from agentgit.services.versions import VersionService

pytestmark = pytest.mark.integration


@pytest.fixture
def versioned(history):
    repository = history.create_repository("版本测试")
    run = history.create_run(repository)
    environment = EnvironmentManifest(
        runtime_version="fixture",
        python_version="3.12",
        code_revision="fixture",
        dependency_lock_digest="a" * 64,
        model_signature="fixture",
        prompt_digest="b" * 64,
        input_ref=history.register_input(repository, b"fixture"),
        evaluation_time="2026-10-21T00:00:00Z",
        random_seed=0,
    )
    commits = []
    for index in range(3):
        result = history.commit(
            repository,
            run,
            expected_head=commits[-1] if commits else None,
            expected_version=index,
            state={"number": index, "plan": {"version": index}},
            environment=environment,
            events=(EventInput("fixture.step", f"step-{index}", {}),),
            artifacts=(ArtifactInput("binary", bytes([255, index]), "application/octet-stream"),),
        )
        commits.append(result.commit_id)
    return VersionService(history), repository, run, commits, environment


def fork(service, repository, run, commit, name):
    return service.fork(
        repository,
        NewBranch(
            source_run_id=str(run),
            from_commit=commit,
            name=name,
            expected_version=0,
        ),
        actor="local-operator",
    )


def test_forks_change_only_their_own_heads_and_preserve_source(versioned):
    service, repository, source, commits, environment = versioned
    original = service.history.branch(repository)
    for name, value in (("A", 17), ("B", 29)):
        created = fork(service, repository, source, commits[1], name)
        assert created["head_commit_id"] == commits[1] and created["version"] == 0
        run = service.history.create_run(repository, name)
        result = service.history.commit(
            repository,
            run,
            expected_head=commits[1],
            expected_version=0,
            state={"number": value, "plan": {"version": value}},
            environment=environment,
            events=(EventInput("fixture.branch", "change", {}),),
            artifacts=(ArtifactInput("binary", bytes([value]), "application/octet-stream"),),
        )
        assert service.history.branch(repository, name)["head_commit_id"] == result.commit_id
    assert service.history.branch(repository) == original
    a = service.history.branch(repository, "A")["head_commit_id"]
    b = service.history.branch(repository, "B")["head_commit_id"]
    difference = service.diff(repository, a, b)
    assert difference["common_ancestor"] == commits[1]
    assert {item["path"] for item in difference["items"]} >= {
        "/state/number",
        "/state/plan/version",
        "/artifacts/binary/object_ref",
    }


def test_equivalent_branch_commits_share_object_without_reinserting_published_parent(versioned):
    service, repository, source, commits, environment = versioned
    outputs = []
    for name in ("equivalent-a", "equivalent-b"):
        fork(service, repository, source, commits[0], name)
        run = service.history.create_run(repository, name)
        result = service.history.commit(
            repository,
            run,
            expected_head=commits[0],
            expected_version=0,
            state={"shared": True},
            environment=environment,
            events=(EventInput("fixture.shared", "step", {}),),
            artifacts=(ArtifactInput("binary", b"same bytes", "application/octet-stream"),),
        )
        outputs.append(result.commit_id)
    assert outputs[0] == outputs[1]
    assert service.history.restore(repository, outputs[0]).data == {"shared": True}
    with service.history.engine.connect() as connection:
        parents = TABLES["commit_parents"]
        assert (
            connection.execute(
                sa.select(sa.func.count())
                .select_from(parents)
                .where(parents.c.commit_id == outputs[0])
            ).scalar_one()
            == 1
        )


def test_source_range_rejects_foreign_branch_and_reversed_endpoints(versioned):
    service, repository, run, commits, _ = versioned
    selected = service.select_range(
        repository,
        RecordedRange(
            source_run_id=str(run),
            from_commit=commits[0],
            until_commit=commits[2],
        ),
    )
    assert selected.commits == tuple(commits) and (selected.from_seq, selected.until_seq) == (1, 3)
    fork(service, repository, run, commits[0], "fork")
    other = service.history.create_run(repository, "fork")
    for source, first, last in ((run, commits[2], commits[0]), (other, commits[0], commits[2])):
        with pytest.raises(AgentGitError) as error:
            service.select_range(
                repository,
                RecordedRange(
                    source_run_id=str(source),
                    from_commit=first,
                    until_commit=last,
                ),
            )
        assert error.value.code == ErrorCode.REPLAY_MISMATCH


def test_checkout_and_pagination_are_read_only_and_tenant_scoped(versioned):
    service, repository, run, commits, _ = versioned
    before = service.history.branch(repository)
    viewed = service.checkout(repository, commits[0])
    assert viewed["snapshot"]["data"]["number"] == 0
    first = service.log(repository, limit=2)
    assert [item["commit_id"] for item in first["items"]] == list(reversed(commits[1:]))
    second = service.log(repository, limit=2, after_commit=first["next_after_commit"])
    assert [item["commit_id"] for item in second["items"]] == [commits[0]]
    assert service.history.branch(repository) == before
    other = service.history.create_repository("其他项目")
    with pytest.raises(AgentGitError) as error:
        service.checkout(other, commits[0])
    assert error.value.code == ErrorCode.NOT_FOUND
    with pytest.raises(AgentGitError) as error:
        fork(service, other, run, commits[0], "leak")
    assert error.value.code == ErrorCode.NOT_FOUND


def test_same_branch_name_competition_creates_one_immutable_provenance(versioned):
    service, repository, run, commits, _ = versioned

    def create(_):
        try:
            fork(service, repository, run, commits[1], "same")
            return "created"
        except AgentGitError as error:
            return error.code.value

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(create, range(2))) == ["created", "version_conflict"]
    table = TABLES["branch_forks"]
    with service.history.engine.connect() as connection:
        assert len(list(connection.execute(sa.select(table)))) == 1
    for mutation in (table.update().values(actor="changed"), table.delete()):
        with pytest.raises(sa.exc.DBAPIError), service.history.engine.begin() as connection:
            connection.execute(mutation)


def add_unresolved(service, repository, run, checkpoint, effect=EffectClass.NON_IDEMPOTENT_WRITE):
    history, call_id = service.history, uuid4()
    request = history.objects.put(ObjectType.BLOB, b"pending")
    intent = history.objects.put_json(
        ObjectType.CALL,
        CallIntent(
            occurrence=CallOccurrence(
                logical_step_id="pending",
                call_index=0,
                attempt=1,
                provider="tool:fake/v1",
                signature_digest="c" * 64,
                request_digest=request.id,
            ),
            effect=effect,
            request_ref=request.id,
            checkpoint=checkpoint,
            max_response_bytes=128,
        ).model_dump(mode="json"),
    )
    with history.engine.begin() as connection:
        history._ref(connection, repository, request, "external_call", str(call_id), "request")
        history._ref(connection, repository, intent, "external_call", str(call_id), "intent")
        connection.execute(
            TABLES["external_calls"]
            .insert()
            .values(
                id=call_id,
                repository_id=repository,
                run_id=run,
                logical_step_id="pending",
                call_index=0,
                attempt=1,
                provider="tool:fake/v1",
                signature_digest="c" * 64,
                request_digest=request.id,
                effect=effect.value,
                intent_ref=intent.id,
                status="started",
            )
        )


def test_unresolved_write_blocks_its_checkpoint_and_descendants_but_not_earlier_state(versioned):
    service, repository, run, commits, _ = versioned
    add_unresolved(service, repository, run, commits[1])
    assert fork(service, repository, run, commits[0], "before")["version"] == 0
    for index in (1, 2):
        with pytest.raises(AgentGitError) as error:
            fork(service, repository, run, commits[index], f"unsafe-{index}")
        assert error.value.code == ErrorCode.FORBIDDEN


def test_diff_without_shared_ancestor_reports_none(versioned):
    service, repository, _, commits, environment = versioned
    with service.history.engine.begin() as connection:
        connection.execute(
            TABLES["branches"].insert().values(repository_id=repository, name="root")
        )
    run = service.history.create_run(repository, "root")
    result = service.history.commit(
        repository,
        run,
        expected_head=None,
        expected_version=0,
        state={"other_root": True},
        environment=environment,
        events=(EventInput("fixture.root", "root", {}),),
    )
    assert service.diff(repository, commits[2], result.commit_id)["common_ancestor"] is None
