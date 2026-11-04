"""四角色固定契约的路由、归属、去重和共享版本；不冒充耐久协作。"""

import itertools
import json
from pathlib import Path
from uuid import UUID

import pytest

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration import (
    CausalEventAddress,
    CollaborationBindings,
    CollaborationMessage,
    CollaborationProgram,
    CollaborationRole,
    DeliveryLedger,
    RoleBinding,
    advance_shared_artifact,
    authorize_message,
    receive_message,
)
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import parse_contract

ROOT = Path(__file__).parents[1] / "fixtures/collaboration/v1"
ROLES = tuple(CollaborationRole)
PATHS = ("evidence.json", "verification.json", "report.md", "sources.json")
KINDS = ("dispatch", "cancel", "evidence", "verification", "result")


def uid(value):
    return str(UUID(int=value))


def ref(value):
    return object_digest(ObjectType.BLOB, encode(value.model_dump(mode="json")))


def changed(value, **updates):
    return parse_contract(encode({**value.model_dump(mode="json"), **updates}), type(value))


@pytest.fixture
def program():
    return parse_contract((ROOT / "program.json").read_bytes(), CollaborationProgram)


@pytest.fixture
def bindings():
    return CollaborationBindings(
        repository_id=uid(1),
        collaboration_id=uid(2),
        agents=tuple(
            RoleBinding(role=role, agent_run_id=uid(100 + i), task_id=uid(200 + i))
            for i, role in enumerate(ROLES)
        ),
    )


def event(bindings, role):
    run = next(a.agent_run_id for a in bindings.agents if a.role == role)
    return CausalEventAddress(
        agent_run_id=run,
        commit_id="a" * 64,
        seq=1,
        payload_ref="b" * 64,
    )


def message(bindings, sender="researcher", receiver="verifier", kind="evidence", number=0):
    source = next(a for a in bindings.agents if a.role == sender)
    target = next(a for a in bindings.agents if a.role == receiver)
    return CollaborationMessage(
        repository_id=bindings.repository_id,
        collaboration_id=bindings.collaboration_id,
        message_id=uid(1000 + number),
        sender=source.agent_run_id,
        receiver=target.agent_run_id,
        task_id=target.task_id,
        correlation_id=bindings.collaboration_id,
        kind=kind,
        payload_ref="c" * 64,
        parent_event_id=event(bindings, sender),
    )


def ledger(program, bindings, **updates):
    return DeliveryLedger(program_ref=ref(program), bindings_ref=ref(bindings), **updates)


def rejected(code, action):
    with pytest.raises(AgentGitError) as caught:
        action()
    assert caught.value.code == code


def test_fixture_is_explicit_four_role_dag_and_independent_budgets(program):
    cases = json.loads((ROOT / "cases.json").read_text("utf-8"))
    assert cases["status"] == "specification_only_not_acceptance"
    assert parse_contract(encode(program.model_dump(mode="json")), CollaborationProgram) == program
    assert [(t.key, t.depends_on) for t in program.tasks] == [
        ("collect", ()),
        ("verify", ("collect",)),
        ("write", ("collect", "verify")),
    ]
    roles = program.model_dump(mode="json")["roles"]
    roles[1]["limits"]["max_tool_calls"] = 1
    reduced = changed(program, roles=roles)
    assert reduced.policy(CollaborationRole.RESEARCHER).limits.max_tool_calls == 1
    assert reduced.policy(CollaborationRole.VERIFIER).limits.max_tool_calls == 8
    assert reduced.policy(CollaborationRole.WRITER).permissions == ("artifact_write", "send_result")
    assert program.policy(CollaborationRole.RESEARCHER).limits.max_tool_calls == 8


@pytest.mark.parametrize(
    "case",
    [
        "missing_role",
        "duplicate_role",
        "unknown_role",
        "expanded_permission",
        "duplicate_permission",
        "terminal_permission",
        "supervisor_tool",
        "replan",
        "cycle",
        "missing_dependency",
        "extra_task",
        "wrong_task_role",
        "protocol",
        "extra_field",
        "script",
    ],
)
def test_program_rejects_unsupported_scope(program, case):
    value = program.model_dump(mode="json")
    match case:
        case "missing_role":
            value["roles"].pop()
        case "duplicate_role":
            value["roles"][0] = value["roles"][1]
        case "unknown_role":
            value["roles"][0]["role"] = "remote-superuser"
        case "expanded_permission":
            value["roles"][1]["permissions"].append("artifact_write")
        case "duplicate_permission":
            value["roles"][1]["permissions"].append("source_read")
        case "terminal_permission":
            value["roles"][1]["permissions"].append("terminal_exec")
        case "supervisor_tool":
            value["roles"][0]["limits"]["max_tool_calls"] = 1
        case "replan":
            value["roles"][1]["limits"]["max_replans"] = 1
        case "cycle":
            value["tasks"][0]["depends_on"] = ["write"]
        case "missing_dependency":
            value["tasks"][2]["depends_on"] = ["verify"]
        case "extra_task":
            value["tasks"].append(value["tasks"][0])
        case "wrong_task_role":
            value["tasks"][0]["role"] = "writer"
        case "protocol":
            value["protocol"] = "agentgit-collaboration/v2"
        case "extra_field":
            value["private_context"] = "不会作为通用字段接纳"
        case "script":
            value["tasks"][0]["script"] = "whoami"
    rejected(ErrorCode.VALIDATION, lambda: parse_contract(encode(value), CollaborationProgram))


@pytest.mark.parametrize("field", ["role", "agent_run_id", "task_id"])
def test_bindings_require_four_distinct_real_identifiers(bindings, field):
    agents = bindings.model_dump(mode="json")["agents"]
    agents[1][field] = agents[0][field]
    rejected(ErrorCode.VALIDATION, lambda: changed(bindings, agents=agents))


@pytest.mark.parametrize(
    "sender,receiver,kind",
    [(a, b, k) for a, b, k in itertools.product(ROLES, ROLES, KINDS) if a != b],
)
def test_all_role_pairs_obey_frozen_routes(program, bindings, sender, receiver, kind):
    routes = json.loads((ROOT / "cases.json").read_text("utf-8"))["routes"]
    incoming = message(bindings, sender, receiver, kind)
    if [kind, sender.value, receiver.value] in routes:
        authorize_message(program, bindings, incoming)
    else:
        rejected(ErrorCode.FORBIDDEN, lambda: authorize_message(program, bindings, incoming))


@pytest.mark.parametrize(
    "case",
    ["project", "collaboration", "task", "run", "parent", "correlation", "seq", "kind", "header"],
)
def test_message_binding_and_exact_envelope_reject_forgery(program, bindings, case):
    original = message(bindings).model_dump(mode="json")
    code = ErrorCode.FORBIDDEN
    match case:
        case "project":
            original["repository_id"] = uid(900)
        case "collaboration":
            original["collaboration_id"] = original["correlation_id"] = uid(900)
        case "task":
            original["task_id"] = bindings.agents[0].task_id
        case "run":
            original["sender"] = original["parent_event_id"]["agent_run_id"] = uid(900)
        case "parent":
            original["parent_event_id"]["agent_run_id"] = original["receiver"]
            code = ErrorCode.VALIDATION
        case "correlation":
            original["correlation_id"] = uid(900)
            code = ErrorCode.VALIDATION
        case "seq":
            original["parent_event_id"]["seq"] = True
            code = ErrorCode.VALIDATION
        case "kind":
            original["kind"] = "grant_permission"
            code = ErrorCode.VALIDATION
        case "header":
            original["actor"] = "trusted-operator"
            code = ErrorCode.VALIDATION
    rejected(
        code,
        lambda: authorize_message(
            program, bindings, parse_contract(encode(original), CollaborationMessage)
        ),
    )


def test_restricted_sender_cannot_send_or_publish(program, bindings):
    roles = program.model_dump(mode="json")["roles"]
    roles[1]["permissions"] = ["corpus_search", "source_read"]
    restricted = changed(program, roles=roles)
    rejected(
        ErrorCode.FORBIDDEN, lambda: authorize_message(restricted, bindings, message(bindings))
    )
    rejected(
        ErrorCode.FORBIDDEN,
        lambda: advance_shared_artifact(
            restricted,
            bindings,
            None,
            path="evidence.json",
            expected_version=0,
            object_ref="c" * 64,
            publisher_event=event(bindings, "researcher"),
        ),
    )


def test_duplicate_is_noop_but_changed_input_conflicts_without_mutating_ledger(program, bindings):
    original = ledger(program, bindings)
    incoming = message(bindings)
    once, applied = receive_message(program, bindings, original, incoming)
    twice, applied_again = receive_message(program, bindings, once, incoming)
    assert applied and not applied_again and twice == once and original.messages == ()
    assert encode(twice.model_dump(mode="json")) == encode(once.model_dump(mode="json"))
    for altered in [changed(incoming, payload_ref="d" * 64), message(bindings, receiver="writer")]:
        rejected(
            ErrorCode.VERSION_CONFLICT,
            lambda altered=altered: receive_message(program, bindings, once, altered),
        )
    assert once.messages == (incoming,)


@pytest.mark.parametrize("field", ["program_ref", "bindings_ref"])
def test_ledger_cannot_move_to_another_program_or_bindings(program, bindings, field):
    bad = changed(ledger(program, bindings), **{field: "e" * 64})
    rejected(
        ErrorCode.FORBIDDEN, lambda: receive_message(program, bindings, bad, message(bindings))
    )


def test_duplicate_cannot_hide_corrupt_later_entries(program, bindings):
    incoming = message(bindings)
    foreign = changed(message(bindings, number=1), repository_id=uid(999))
    bad = ledger(program, bindings, messages=(incoming, foreign))
    rejected(ErrorCode.FORBIDDEN, lambda: receive_message(program, bindings, bad, incoming))


def test_message_count_bound_is_checked_after_duplicate(program, bindings):
    messages = tuple(message(bindings, number=i) for i in range(32))
    full = ledger(program, bindings, messages=messages)
    assert receive_message(program, bindings, full, messages[0]) == (full, False)
    rejected(
        ErrorCode.BUDGET_EXHAUSTED,
        lambda: receive_message(program, bindings, full, message(bindings, number=32)),
    )


@pytest.mark.parametrize("role,path", list(itertools.product(ROLES, PATHS)))
def test_shared_paths_enforce_role_permissions(program, bindings, role, path):
    allowed = (
        (role == CollaborationRole.RESEARCHER and path == "evidence.json")
        or (role == CollaborationRole.VERIFIER and path in ("evidence.json", "verification.json"))
        or (role == CollaborationRole.WRITER and path in ("report.md", "sources.json"))
    )

    def action():
        return advance_shared_artifact(
            program,
            bindings,
            None,
            path=path,
            expected_version=0,
            object_ref="c" * 64,
            publisher_event=event(bindings, role),
        )

    if allowed:
        assert action().version == 1
    else:
        rejected(ErrorCode.FORBIDDEN, action)


def test_verified_shared_revision_preserves_source_and_rejects_stale_researcher(program, bindings):
    first = advance_shared_artifact(
        program,
        bindings,
        None,
        path="evidence.json",
        expected_version=0,
        object_ref="c" * 64,
        publisher_event=event(bindings, "researcher"),
    )
    second = advance_shared_artifact(
        program,
        bindings,
        first,
        path="evidence.json",
        expected_version=1,
        object_ref="d" * 64,
        publisher_event=event(bindings, "verifier"),
    )
    assert first.version == 1 and first.object_ref == "c" * 64 and second.version == 2
    assert second.publisher_event.agent_run_id == bindings.agents[2].agent_run_id
    rejected(
        ErrorCode.VERSION_CONFLICT,
        lambda: advance_shared_artifact(
            program,
            bindings,
            second,
            path="evidence.json",
            expected_version=1,
            object_ref="e" * 64,
            publisher_event=event(bindings, "researcher"),
        ),
    )


@pytest.mark.parametrize(
    "field",
    ["repository_id", "collaboration_id", "program_ref", "bindings_ref", "path", "publisher_event"],
)
def test_shared_head_cannot_import_another_scope_or_publisher(program, bindings, field):
    head = advance_shared_artifact(
        program,
        bindings,
        None,
        path="report.md",
        expected_version=0,
        object_ref="c" * 64,
        publisher_event=event(bindings, "writer"),
    )
    value = {
        "repository_id": uid(999),
        "collaboration_id": uid(999),
        "program_ref": "e" * 64,
        "bindings_ref": "e" * 64,
        "path": "sources.json",
        "publisher_event": event(bindings, "researcher").model_dump(mode="json"),
    }[field]
    foreign = changed(head, **{field: value})
    rejected(
        ErrorCode.FORBIDDEN,
        lambda: advance_shared_artifact(
            program,
            bindings,
            foreign,
            path="report.md",
            expected_version=1,
            object_ref="d" * 64,
            publisher_event=event(bindings, "writer"),
        ),
    )


@pytest.mark.parametrize("version", [True, -1, 1.0, 9_007_199_254_740_991])
def test_shared_version_requires_exact_safe_integer(program, bindings, version):
    rejected(
        ErrorCode.VALIDATION,
        lambda: advance_shared_artifact(
            program,
            bindings,
            None,
            path="report.md",
            expected_version=version,
            object_ref="c" * 64,
            publisher_event=event(bindings, "writer"),
        ),
    )
