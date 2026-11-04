"""四角色固定程序、消息归属和共享版本的纯契约；耐久授权仍由服务核对。"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import Contract, Digest, Identifier, NonNegative, parse_contract
from agentgit.core.research import BudgetLimits, ResearchGoal


class CollaborationRole(StrEnum):
    SUPERVISOR = "supervisor"
    RESEARCHER = "researcher"
    VERIFIER = "verifier"
    WRITER = "writer"


Permission = Literal[
    "delegate",
    "cancel_children",
    "corpus_search",
    "source_read",
    "share_evidence",
    "verify_evidence",
    "share_verification",
    "artifact_write",
    "send_result",
]
TaskKey = Literal["collect", "verify", "write"]
MessageKind = Literal["dispatch", "cancel", "evidence", "verification", "result"]
SharedPath = Literal["evidence.json", "verification.json", "report.md", "sources.json"]

_PERMISSIONS: dict[CollaborationRole, frozenset[str]] = {
    CollaborationRole.SUPERVISOR: frozenset({"delegate", "cancel_children"}),
    CollaborationRole.RESEARCHER: frozenset({"corpus_search", "source_read", "share_evidence"}),
    CollaborationRole.VERIFIER: frozenset({"source_read", "verify_evidence", "share_verification"}),
    CollaborationRole.WRITER: frozenset({"artifact_write", "send_result"}),
}
_TASKS = {
    "collect": (CollaborationRole.RESEARCHER, ()),
    "verify": (CollaborationRole.VERIFIER, ("collect",)),
    "write": (CollaborationRole.WRITER, ("collect", "verify")),
}
_SEND_PERMISSION = {
    "dispatch": "delegate",
    "cancel": "cancel_children",
    "evidence": "share_evidence",
    "verification": "share_verification",
    "result": "send_result",
}


class RolePolicy(Contract):
    role: CollaborationRole
    permissions: tuple[Permission, ...]
    limits: BudgetLimits

    @model_validator(mode="after")
    def restricted_permissions(self) -> "RolePolicy":
        if len(set(self.permissions)) != len(self.permissions) or (
            set(self.permissions) - _PERMISSIONS[self.role]
        ):
            raise ValueError("角色权限不能重复或超出声明的固定能力")
        if self.role == CollaborationRole.SUPERVISOR and self.limits.max_tool_calls != 0:
            raise ValueError("Supervisor 不直接调用研究或执行工具")
        if self.limits.max_replans != 0:
            raise ValueError("固定协作 DAG 不接受动态重规划")
        return self


class CollaborationTask(Contract):
    key: TaskKey
    role: CollaborationRole
    depends_on: tuple[TaskKey, ...]


class CollaborationProgram(Contract):
    kind: Literal["offline-four-role-research/v1"] = "offline-four-role-research/v1"
    protocol: Literal["agentgit-collaboration/v1"] = "agentgit-collaboration/v1"
    goal: ResearchGoal
    roles: Annotated[tuple[RolePolicy, ...], Field(min_length=4, max_length=4)]
    tasks: Annotated[tuple[CollaborationTask, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def fixed_dag(self) -> "CollaborationProgram":
        if {policy.role for policy in self.roles} != set(CollaborationRole):
            raise ValueError("协作必须声明四个不同角色")
        tasks = {task.key: (task.role, task.depends_on) for task in self.tasks}
        if tasks != _TASKS:
            raise ValueError("仅支持明确声明的 collect/verify/write 依赖 DAG")
        return self

    def policy(self, role: CollaborationRole) -> RolePolicy:
        return next(policy for policy in self.roles if policy.role == role)


class RoleBinding(Contract):
    role: CollaborationRole
    agent_run_id: Identifier
    task_id: Identifier


class CollaborationBindings(Contract):
    repository_id: Identifier
    collaboration_id: Identifier
    agents: Annotated[tuple[RoleBinding, ...], Field(min_length=4, max_length=4)]

    @model_validator(mode="after")
    def independent_runs(self) -> "CollaborationBindings":
        if (
            {agent.role for agent in self.agents} != set(CollaborationRole)
            or len({agent.agent_run_id for agent in self.agents}) != 4
            or len({agent.task_id for agent in self.agents}) != 4
        ):
            raise ValueError("四角色必须绑定不同 Run、任务和角色")
        return self

    def agent(self, run_id: str) -> RoleBinding:
        for agent in self.agents:
            if agent.agent_run_id == run_id:
                return agent
        raise AgentGitError(ErrorCode.FORBIDDEN, "Run 不属于本协作")


class CausalEventAddress(Contract):
    agent_run_id: Identifier
    commit_id: Digest
    seq: NonNegative
    payload_ref: Digest


class CollaborationMessage(Contract):
    repository_id: Identifier
    collaboration_id: Identifier
    message_id: Identifier
    sender: Identifier
    receiver: Identifier
    task_id: Identifier
    correlation_id: Identifier
    kind: MessageKind
    payload_ref: Digest
    parent_event_id: CausalEventAddress

    @model_validator(mode="after")
    def parent_sender(self) -> "CollaborationMessage":
        if self.sender == self.receiver or self.parent_event_id.agent_run_id != self.sender:
            raise ValueError("消息父事件必须属于发送 Run，接收方必须不同")
        if self.correlation_id != self.collaboration_id:
            raise ValueError("消息 correlation_id 必须固定为本协作 ID")
        return self


def _checked[T: Contract](value: T) -> T:
    return parse_contract(encode(value.model_dump(mode="json")), type(value))


def _ref(value: Contract) -> str:
    return object_digest(ObjectType.BLOB, encode(value.model_dump(mode="json")))


def authorize_message(
    program: CollaborationProgram,
    bindings: CollaborationBindings,
    message: CollaborationMessage,
) -> None:
    """仅核对冻结路由/归属；真实调用身份、父事件与 payload 字节须再查耐久记录。"""
    program, bindings, message = _checked(program), _checked(bindings), _checked(message)
    if (message.repository_id, message.collaboration_id) != (
        bindings.repository_id,
        bindings.collaboration_id,
    ):
        raise AgentGitError(ErrorCode.FORBIDDEN, "消息不属于本项目协作")
    sender, receiver = bindings.agent(message.sender), bindings.agent(message.receiver)
    if message.task_id != receiver.task_id:
        raise AgentGitError(ErrorCode.FORBIDDEN, "消息必须绑定接收角色的实际任务")
    source, target = sender.role, receiver.role
    allowed = (
        (
            message.kind in ("dispatch", "cancel")
            and source == CollaborationRole.SUPERVISOR
            and target != CollaborationRole.SUPERVISOR
        )
        or (
            message.kind == "evidence"
            and source == CollaborationRole.RESEARCHER
            and target in (CollaborationRole.VERIFIER, CollaborationRole.WRITER)
        )
        or (
            message.kind == "verification"
            and source == CollaborationRole.VERIFIER
            and target == CollaborationRole.WRITER
        )
        or (
            message.kind == "result"
            and source == CollaborationRole.WRITER
            and target == CollaborationRole.SUPERVISOR
        )
    )
    if not allowed or _SEND_PERMISSION[message.kind] not in program.policy(source).permissions:
        raise AgentGitError(ErrorCode.FORBIDDEN, "消息路由或发送角色权限不符")


class DeliveryLedger(Contract):
    program_ref: Digest
    bindings_ref: Digest
    messages: Annotated[tuple[CollaborationMessage, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def unique_messages(self) -> "DeliveryLedger":
        if len({message.message_id for message in self.messages}) != len(self.messages):
            raise ValueError("接收账本不能包含重复 message_id")
        return self


def receive_message(
    program: CollaborationProgram,
    bindings: CollaborationBindings,
    ledger: DeliveryLedger,
    message: CollaborationMessage,
) -> tuple[DeliveryLedger, bool]:
    """纯候选去重；服务必须在接收推进的同一事务内保存收据和检查点。"""
    program, bindings, ledger = _checked(program), _checked(bindings), _checked(ledger)
    message = _checked(message)
    if (ledger.program_ref, ledger.bindings_ref) != (_ref(program), _ref(bindings)):
        raise AgentGitError(ErrorCode.FORBIDDEN, "消息账本与冻结程序/角色绑定不符")
    authorize_message(program, bindings, message)
    for existing in ledger.messages:
        authorize_message(program, bindings, existing)
    for existing in ledger.messages:
        if existing.message_id == message.message_id:
            if existing != message:
                raise AgentGitError(ErrorCode.VERSION_CONFLICT, "相同消息 ID 不能改变输入")
            return ledger, False
    if len(ledger.messages) == 32:
        raise AgentGitError(ErrorCode.BUDGET_EXHAUSTED, "协作消息超过固定上限")
    return DeliveryLedger(
        program_ref=ledger.program_ref,
        bindings_ref=ledger.bindings_ref,
        messages=(*ledger.messages, message),
    ), True


class SharedArtifactHead(Contract):
    repository_id: Identifier
    collaboration_id: Identifier
    program_ref: Digest
    bindings_ref: Digest
    path: SharedPath
    version: Annotated[int, Field(ge=1, le=9_007_199_254_740_991)]
    object_ref: Digest
    publisher_event: CausalEventAddress


def _authorize_publication(
    program: CollaborationProgram,
    bindings: CollaborationBindings,
    event: CausalEventAddress,
    path: SharedPath,
) -> None:
    publisher = bindings.agent(event.agent_run_id)
    permissions = {
        (CollaborationRole.RESEARCHER, "evidence.json"): "share_evidence",
        (CollaborationRole.VERIFIER, "evidence.json"): "share_verification",
        (CollaborationRole.VERIFIER, "verification.json"): "share_verification",
        (CollaborationRole.WRITER, "report.md"): "artifact_write",
        (CollaborationRole.WRITER, "sources.json"): "artifact_write",
    }
    required = permissions.get((publisher.role, path))
    if required is None or required not in program.policy(publisher.role).permissions:
        raise AgentGitError(ErrorCode.FORBIDDEN, "角色不能发布此共享产物")


def advance_shared_artifact(
    program: CollaborationProgram,
    bindings: CollaborationBindings,
    current: SharedArtifactHead | None,
    *,
    path: SharedPath,
    expected_version: int,
    object_ref: str,
    publisher_event: CausalEventAddress,
) -> SharedArtifactHead:
    """纯乐观锁候选；尚不读取对象或在数据库发布共享版本。"""
    program, bindings = _checked(program), _checked(bindings)
    publisher_event = _checked(publisher_event)
    _authorize_publication(program, bindings, publisher_event, path)
    if type(expected_version) is not int or not 0 <= expected_version < 9_007_199_254_740_991:
        raise AgentGitError(ErrorCode.VALIDATION, "共享期望版本必须为有界整数")
    if current is not None:
        current = _checked(current)
        if (
            current.repository_id,
            current.collaboration_id,
            current.program_ref,
            current.bindings_ref,
            current.path,
        ) != (
            bindings.repository_id,
            bindings.collaboration_id,
            _ref(program),
            _ref(bindings),
            path,
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "共享产物不属于本程序/协作/路径")
        _authorize_publication(program, bindings, current.publisher_event, current.path)
    if (current.version if current is not None else 0) != expected_version:
        raise AgentGitError(ErrorCode.VERSION_CONFLICT, "共享产物版本已改变")
    return SharedArtifactHead(
        repository_id=bindings.repository_id,
        collaboration_id=bindings.collaboration_id,
        program_ref=_ref(program),
        bindings_ref=_ref(bindings),
        path=path,
        version=expected_version + 1,
        object_ref=object_ref,
        publisher_event=publisher_event,
    )
