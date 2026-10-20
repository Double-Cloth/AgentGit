"""版本化产物使用跨平台安全的 POSIX 相对路径。"""

from collections.abc import Iterable

from agentgit.core.errors import AgentGitError, ErrorCode

_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


def validate_artifact_paths(paths: Iterable[str]) -> None:
    seen: dict[str, str] = {}
    files: set[str] = set()
    directories: set[str] = set()
    for path in paths:
        if (
            not path
            or len(path) > 240
            or path.startswith("/")
            or any(char in path for char in '\\:<>"|?*')
            or any(ord(char) < 32 for char in path)
        ):
            raise AgentGitError(ErrorCode.VALIDATION, "产物路径不是安全相对路径")
        parts = path.split("/")
        for index, part in enumerate(parts):
            if (
                part in ("", ".", "..")
                or part.endswith((" ", "."))
                or part.split(".")[0].upper() in _DEVICES
            ):
                raise AgentGitError(ErrorCode.VALIDATION, "产物路径含保留名或非法分段")
            prefix = "/".join(parts[: index + 1])
            key = prefix.casefold()
            if key in seen and seen[key] != prefix:
                raise AgentGitError(ErrorCode.VALIDATION, "产物路径存在大小写冲突")
            seen[key] = prefix
            if index < len(parts) - 1:
                if key in files:
                    raise AgentGitError(ErrorCode.VALIDATION, "产物文件与目录冲突")
                directories.add(key)
        key = path.casefold()
        if key in files or key in directories:
            raise AgentGitError(ErrorCode.VALIDATION, "产物路径重复或与目录冲突")
        files.add(key)
