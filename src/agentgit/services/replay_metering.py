"""读取本次真实模型调用的项目内计量证据，排除恢复复用和 Hybrid 观测。"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa

from agentgit.core.canonical import ObjectType
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.models import CallIntent, parse_contract
from agentgit.core.research import SavedCallOutcome
from agentgit.experiments.metering import ModelObservation, model_usage
from agentgit.services.history import TABLES, HistoryService


def physical_model_usage(
    history: HistoryService, repository: UUID, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if len(rows) > 1000:
        raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "本次真实模型调用超过冻结预算上限")
    refs = TABLES["object_refs"]
    with history.engine.connect() as connection:
        resolutions = TABLES["call_resolutions"]
        resolved = (
            {
                row["call_id"]: row
                for row in connection.execute(
                    sa.select(resolutions).where(
                        resolutions.c.repository_id == repository,
                        resolutions.c.call_id.in_([row["id"] for row in rows]),
                    )
                ).mappings()
            }
            if rows
            else {}
        )
        owned = (
            set(
                connection.execute(
                    sa.select(
                        refs.c.owner_type, refs.c.owner_id, refs.c.role, refs.c.object_id
                    ).where(
                        refs.c.repository_id == repository,
                        sa.or_(
                            sa.and_(
                                refs.c.owner_type == "external_call",
                                refs.c.owner_id.in_([str(row["id"]) for row in rows]),
                            ),
                            sa.and_(
                                refs.c.owner_type == "call_resolution",
                                refs.c.owner_id.in_([str(row["id"]) for row in resolved.values()]),
                            ),
                        ),
                    )
                ).tuples()
            )
            if rows
            else set()
        )

    def read(row: Mapping[str, Any], role: str, identity: str, kind: ObjectType) -> bytes:
        owner_type, owner_id = "external_call", str(row["id"])
        resolution = resolved.get(row["id"])
        if role in ("result", "response") and resolution is not None:
            if resolution["result_ref"] != row["result_ref"]:
                raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "模型核对结果与原调用索引不符")
            owner_type, owner_id = "call_resolution", str(resolution["id"])
        if (owner_type, owner_id, role, identity) not in owned:
            raise AgentGitError(ErrorCode.RECORDING_MISSING, "模型计量证据缺少当前项目调用归属")
        return history.objects.get(identity, kind)

    records = []
    for row in rows:
        intent = parse_contract(read(row, "intent", row["intent_ref"], ObjectType.CALL), CallIntent)
        if (
            intent.occurrence.provider != row["provider"]
            or intent.occurrence.signature_digest != row["signature_digest"]
            or intent.request_ref != row["request_digest"]
            or intent.occurrence.request_digest != row["request_digest"]
            or ("external_call", str(row["id"]), "request", intent.request_ref) not in owned
            or type(row["input_bytes"]) is not int
        ):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "模型计量意图与调用索引或输入元数据不符")
        saved = None
        payload = None
        if row["result_ref"] is not None:
            saved = parse_contract(
                read(row, "result", row["result_ref"], ObjectType.CALL), SavedCallOutcome
            )
            if intent.model_budget is not None:
                payload = read(row, "response", saved.response_ref, ObjectType.BLOB)
                if saved.complete and len(payload) != saved.response_bytes:
                    raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "模型计量响应长度与耐久结果不符")
        elif row["status"] not in ("started", "outcome_unknown"):
            raise AgentGitError(ErrorCode.OBJECT_CORRUPT, "已知模型结果缺少耐久响应")
        records.append(ModelObservation(intent, row["input_bytes"], saved, payload, row["status"]))
    return model_usage(records)
