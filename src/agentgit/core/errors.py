"""面向 CLI、API 和恢复逻辑的稳定错误。"""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"
    INTERNAL_ERROR = "internal_error"
    APPROVAL_REQUIRED = "approval_required"
    CONTROL_REQUESTED = "control_requested"
    VERSION_CONFLICT = "version_conflict"
    REPLAY_MISMATCH = "replay_mismatch"
    RECORDING_MISSING = "recording_missing"
    BUDGET_EXHAUSTED = "budget_exhausted"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    OBJECT_CORRUPT = "object_corrupt"
    OUTPUT_LIMIT = "output_limit"
    CONFIGURATION = "configuration"
    PLAN_INVALID = "plan_invalid"
    MODEL_EMPTY = "model_empty"
    MODEL_ERROR = "model_error"
    TOOL_EMPTY = "tool_empty"
    TOOL_ERROR = "tool_error"
    LOOP_DETECTED = "loop_detected"
    VALIDATION_FAILED = "validation_failed"
    CONTEXT_LIMIT = "context_limit"


class AgentGitError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}
