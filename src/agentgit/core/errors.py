"""面向 CLI、API 和恢复逻辑的稳定错误。"""

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
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


class AgentGitError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": self.message}
