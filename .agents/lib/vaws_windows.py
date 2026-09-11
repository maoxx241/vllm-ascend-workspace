"""Windows process checks and pre-dependency interpreter lifetime."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import subprocess
import sys


def pid_alive(pid: int) -> bool:
    """Observe a Windows process without sending it a termination signal."""
    if pid <= 0:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel.WaitForSingleObject(handle, 0) == 0x102
    finally:
        kernel.CloseHandle(handle)


def run_owned(command: list[str], *, env: dict[str, str]) -> int:
    """Wait for a child; abrupt launcher termination closes its whole job tree.

    Start suspended so even the Windows venv redirector cannot launch children
    outside the job. All Win32 declarations preserve 64-bit process handles.
    Only used before the installed workspace packages can be imported.
    """
    size_t = ctypes.c_size_t

    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", size_t),
                    ("max_working_set", size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", size_t), ("priority", wintypes.DWORD),
                    ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                    ("process_memory", size_t), ("job_memory", size_t),
                    ("peak_process_memory", size_t), ("peak_job_memory", size_t)]

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("thread_id", wintypes.DWORD), ("owner_pid", wintypes.DWORD),
                    ("base_priority", wintypes.LONG), ("delta_priority", wintypes.LONG),
                    ("flags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def api(name, restype, *argtypes):
        function = getattr(kernel, name)
        function.restype, function.argtypes = restype, argtypes
        return function

    create_job = api("CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    set_limits = api("SetInformationJobObject", wintypes.BOOL, wintypes.HANDLE,
                     ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    assign_job = api("AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
    open_process = api("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    snapshot = api("CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    first_thread = api("Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    next_thread = api("Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(ThreadEntry))
    open_thread = api("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    resume_thread = api("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
    close = api("CloseHandle", wintypes.BOOL, wintypes.HANDLE)

    def check(value):
        if not value:
            raise ctypes.WinError(ctypes.get_last_error())
        return value

    job = check(create_job(None, None))
    process = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        check(set_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        process = subprocess.Popen(command, env=env, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                                   creationflags=subprocess.CREATE_NO_WINDOW | 0x00000004)  # CREATE_SUSPENDED
        handle = check(open_process(0x0101, False, process.pid))  # SET_QUOTA | TERMINATE
        try:
            check(assign_job(job, handle))
        finally:
            close(handle)
        threads = snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
        if threads == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            row = ThreadEntry()
            row.size = ctypes.sizeof(row)
            more = first_thread(threads, ctypes.byref(row))
            while more and row.owner_pid != process.pid:
                more = next_thread(threads, ctypes.byref(row))
            if not more:
                raise RuntimeError("cannot find the suspended interpreter's primary thread")
            thread = check(open_thread(0x0002, False, row.thread_id))  # SUSPEND_RESUME
            try:
                if resume_thread(thread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                close(thread)
        finally:
            close(threads)
        result = process.wait()
        # A normal CLI exit must preserve package-owned detached services
        # (coordinator, knowledge, monitor). Abrupt parent termination skips
        # this step and the kernel closes every still-owned descendant.
        limits.basic.flags = 0
        check(set_limits(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        return result
    finally:
        close(job)
        if process is not None:
            if process.poll() is None:
                process.kill()  # Also covers assignment failure before resume.
            process.wait(timeout=5)
