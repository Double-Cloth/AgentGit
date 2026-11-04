"""实际经过调用网关的离线角色模型；只返回固定任务决策，不获得执行能力。"""

from agentgit.core.canonical import ObjectType, encode, object_digest
from agentgit.core.collaboration_execution import RoleDecision, RoleModelRequest
from agentgit.core.models import parse_contract


class MockCollaborationModel:
    signature = object_digest(
        ObjectType.CALL,
        encode(
            {
                "schema_version": 1,
                "model": "mock-collaboration/v1",
                "request": RoleModelRequest.model_json_schema(),
                "reply": RoleDecision.model_json_schema(),
            }
        ),
    )

    def invoke(self, payload: bytes) -> bytes:
        request = parse_contract(payload, RoleModelRequest)
        return encode(
            RoleDecision(role=request.role, action=request.action).model_dump(mode="json")
        )
