"""通过 SDK 公共接口重建的有界日志；持久化回调成功后才确认原生写入。"""

from collections.abc import Callable, Iterator, Sequence
from threading import RLock
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.memory import InMemorySaver

from agentgit.adapters.langgraph import LangGraphAGJSONSerde, check_framework_version
from agentgit.core.canonical import decode, encode
from agentgit.core.errors import AgentGitError, ErrorCode
from agentgit.core.langgraph import LANGGRAPH_CHECKPOINT_VERSION, LANGGRAPH_VERSION

MAX_JOURNAL_BYTES = 8_388_608
MAX_JOURNAL_OPERATIONS = 64


def _invalid(message: str) -> AgentGitError:
    return AgentGitError(ErrorCode.UNSUPPORTED_SCHEMA, message)


def _uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


class LangGraphCheckpointJournal(BaseCheckpointSaver[str]):
    """每次保存完整 AGJSON 日志；History、租约及事务由注入的回调负责。

    只提供首批同步单 thread/空 namespace 的公共 API，不保存 SDK 私有字典。
    回调抛错后实例不可再执行，避免数据库已经提交但回执丢失时沿旧索引推进。
    """

    def __init__(
        self,
        thread_id: str,
        persist: Callable[[bytes, str, str], None],
        *,
        restored: bytes | None = None,
    ):
        check_framework_version()
        if not _uuid(thread_id):
            raise _invalid("LangGraph 日志 thread 必须是规范 UUID")
        super().__init__(serde=LangGraphAGJSONSerde())
        self.thread_id = thread_id
        self._persist = persist
        self._lock = RLock()
        self._failed = False
        empty = {
            "schema_version": 1,
            "kind": "langgraph_checkpoint_journal/v1",
            "framework_version": LANGGRAPH_VERSION,
            "checkpoint_version": LANGGRAPH_CHECKPOINT_VERSION,
            "thread_id": thread_id,
            "operations": [],
        }
        self._raw = restored if restored is not None else encode(empty)
        self._inner = self._restore(self._raw)

    def _config(self, config: RunnableConfig) -> RunnableConfig:
        values = config.get("configurable", {})
        if (
            not isinstance(values, dict)
            or values.get("thread_id") != self.thread_id
            or values.get("checkpoint_ns", "") != ""
        ):
            raise AgentGitError(ErrorCode.FORBIDDEN, "LangGraph 日志不能访问其他 thread/namespace")
        selected: dict[str, Any] = {"thread_id": self.thread_id, "checkpoint_ns": ""}
        if "checkpoint_id" in values:
            if not isinstance(values["checkpoint_id"], str) or not _uuid(values["checkpoint_id"]):
                raise _invalid("LangGraph 原生 checkpoint ID 无效")
            selected["checkpoint_id"] = values["checkpoint_id"]
        return {"configurable": selected}

    def _active(self) -> None:
        if self._failed:
            raise AgentGitError(ErrorCode.VERSION_CONFLICT, "原生保存未确认，须从持久历史重新接管")

    def _restore(self, raw: bytes) -> InMemorySaver:
        if len(raw) > MAX_JOURNAL_BYTES:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "LangGraph checkpoint 日志超过字节上限")
        bundle = decode(raw)
        if (
            not isinstance(bundle, dict)
            or set(bundle)
            != {
                "schema_version",
                "kind",
                "framework_version",
                "checkpoint_version",
                "thread_id",
                "operations",
            }
            or type(bundle["schema_version"]) is not int
            or bundle["schema_version"] != 1
            or bundle["kind"] != "langgraph_checkpoint_journal/v1"
            or bundle["framework_version"] != LANGGRAPH_VERSION
            or bundle["checkpoint_version"] != LANGGRAPH_CHECKPOINT_VERSION
            or bundle["thread_id"] != self.thread_id
            or not isinstance(bundle["operations"], list)
        ):
            raise _invalid("LangGraph 原生日志版本或所属 thread 不匹配")
        operations = bundle["operations"]
        if len(operations) > MAX_JOURNAL_OPERATIONS:
            raise AgentGitError(ErrorCode.OUTPUT_LIMIT, "LangGraph checkpoint 操作超过上限")
        inner = InMemorySaver(serde=LangGraphAGJSONSerde())
        seen: set[str] = set()
        for operation in operations:
            if not isinstance(operation, dict) or not isinstance(operation.get("config"), dict):
                raise _invalid("LangGraph 原生操作格式无效")
            config = self._config(operation["config"])
            if operation["config"] != config:
                raise _invalid("LangGraph 持久配置包含未声明字段")
            parent = config["configurable"].get("checkpoint_id")
            if parent is not None and parent not in seen:
                raise _invalid("LangGraph 原生操作缺少先前 checkpoint")
            if operation.get("kind") == "checkpoint":
                if set(operation) != {"kind", "config", "checkpoint", "metadata", "new_versions"}:
                    raise _invalid("LangGraph checkpoint 操作字段无效")
                checkpoint, metadata, versions = (
                    operation["checkpoint"],
                    operation["metadata"],
                    operation["new_versions"],
                )
                if (
                    not isinstance(checkpoint, dict)
                    or set(checkpoint)
                    != {
                        "v",
                        "id",
                        "ts",
                        "channel_values",
                        "channel_versions",
                        "versions_seen",
                        "updated_channels",
                    }
                    or type(checkpoint.get("v")) is not int
                    or checkpoint["v"] != 4
                    or not isinstance(checkpoint.get("id"), str)
                    or not _uuid(checkpoint["id"])
                    or checkpoint["id"] in seen
                    or not isinstance(checkpoint.get("ts"), str)
                    or not isinstance(checkpoint.get("channel_values"), dict)
                    or not isinstance(checkpoint.get("channel_versions"), dict)
                    or not isinstance(checkpoint.get("versions_seen"), dict)
                    or (
                        checkpoint.get("updated_channels") is not None
                        and (
                            not isinstance(checkpoint["updated_channels"], list)
                            or any(
                                not isinstance(key, str) for key in checkpoint["updated_channels"]
                            )
                        )
                    )
                    or any(
                        not isinstance(value, str)
                        for value in checkpoint["channel_versions"].values()
                    )
                    or any(
                        not isinstance(values, dict)
                        or any(not isinstance(version, str) for version in values.values())
                        for values in checkpoint["versions_seen"].values()
                    )
                    or not isinstance(metadata, dict)
                    or metadata.get("thread_id", self.thread_id) != self.thread_id
                    or not isinstance(versions, dict)
                    or any(not isinstance(value, str) for value in versions.values())
                    or any(
                        checkpoint["channel_versions"].get(key) != value
                        for key, value in versions.items()
                    )
                ):
                    raise _invalid("LangGraph checkpoint 内容、版本或来源无效")
                inner.put(
                    config,
                    cast(Checkpoint, checkpoint),
                    cast(CheckpointMetadata, metadata),
                    versions,
                )
                restored = inner.get_tuple(
                    {"configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}}
                )
                if restored is None or encode(restored.checkpoint) != encode(checkpoint):
                    raise _invalid("LangGraph checkpoint 缺少对应 channel version 的值")
                seen.add(checkpoint["id"])
            elif operation.get("kind") == "writes":
                if set(operation) != {"kind", "config", "writes", "task_id", "task_path"}:
                    raise _invalid("LangGraph pending writes 操作字段无效")
                writes = operation["writes"]
                if (
                    parent is None
                    or not isinstance(writes, list)
                    or len(writes) > 32
                    or any(
                        not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str)
                        for item in writes
                    )
                    or not isinstance(operation["task_id"], str)
                    or not _uuid(operation["task_id"])
                    or not isinstance(operation["task_path"], str)
                    or len(operation["task_path"]) > 1024
                ):
                    raise _invalid("LangGraph pending writes 内容或任务身份无效")
                inner.put_writes(
                    config,
                    [(key, value) for key, value in writes],
                    operation["task_id"],
                    operation["task_path"],
                )
            else:
                raise _invalid("LangGraph 原生操作类型不受支持")
        return inner

    def _save(self, operation: dict[str, Any], native_id: str) -> None:
        self._active()
        bundle = decode(self._raw)
        bundle["operations"].append(operation)
        raw = encode(bundle)
        candidate = self._restore(raw)
        try:
            self._persist(raw, operation["kind"], native_id)
        except BaseException:
            self._failed = True
            raise
        self._raw, self._inner = raw, candidate

    def export(self) -> bytes:
        with self._lock:
            return self._raw

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        with self._lock:
            self._active()
            return self._inner.get_tuple(self._config(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        with self._lock:
            self._active()
            selected = (
                self._config(config)
                if config is not None
                else {"configurable": {"thread_id": self.thread_id, "checkpoint_ns": ""}}
            )
            frames = list(
                self._inner.list(
                    selected,
                    filter=filter,
                    before=self._config(before) if before is not None else None,
                    limit=limit,
                )
            )
        return iter(frames)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        with self._lock:
            selected = self._config(config)
            # 不把 callbacks、凭据或 SDK 运行配置写入不可变日志。
            self._save(
                {
                    "kind": "checkpoint",
                    "config": selected,
                    "checkpoint": checkpoint,
                    "metadata": metadata,
                    "new_versions": new_versions,
                },
                checkpoint["id"],
            )
            return {"configurable": {**selected["configurable"], "checkpoint_id": checkpoint["id"]}}

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        with self._lock:
            selected = self._config(config)
            self._save(
                {
                    "kind": "writes",
                    "config": selected,
                    "writes": [[key, value] for key, value in writes],
                    "task_id": task_id,
                    "task_path": task_path,
                },
                selected["configurable"]["checkpoint_id"],
            )

    def get_next_version(self, current: str | None, channel: None) -> str:
        with self._lock:
            self._active()
            return self._inner.get_next_version(current, channel)
