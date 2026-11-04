"""自有可信 Server 的生命周期；不提供任意命令执行接口。"""

import ctypes
import os
import signal
import subprocess
import sys
from contextlib import suppress
from ctypes import wintypes
from typing import Any


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IOCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class OwnedMCPProcess:
    def __init__(self, process: subprocess.Popen[bytes]):
        self.process = process
        self.job: Any = None
        self.api: Any = None
        if sys.platform != "win32":
            return
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        job = api.CreateJobObjectW(None, None)
        if not job:
            raise OSError("不能创建 MCP 自有进程 Job")
        info = _ExtendedLimits()
        # 仅允许已审阅的单进程 Server，关闭自有句柄时清理全部受管进程。
        info.BasicLimitInformation.LimitFlags = 0x00000008 | 0x00002000
        info.BasicLimitInformation.ActiveProcessLimit = 1
        native_process: Any = process
        if not api.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not api.AssignProcessToJobObject(job, int(native_process._handle)):
            api.CloseHandle(job)
            raise OSError("不能应用 MCP 自有进程限制")
        self.job, self.api = job, api

    def kill(self) -> None:
        if self.job is not None:
            self.api.TerminateJobObject(self.job, 1)
        elif sys.platform != "win32":
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
        elif self.process.poll() is None:
            self.process.kill()

    def close(self) -> None:
        self.kill()
        if self.job is not None:
            self.api.CloseHandle(self.job)
            self.job = None
        self.process.wait(timeout=2)
