"""Platform layer: the few things about processes, privileges and files that differ between
macOS and Windows. Everything else in pogo is plain, portable Python."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

MACHINE = "PC" if IS_WINDOWS else "Mac"
PASSWORD_STORE = "Windows Credential Manager" if IS_WINDOWS else "macOS Keychain"

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    _shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    _shell32.ShellExecuteExW.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_CANCELLED = 1223
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF


# --- running helper commands -------------------------------------------------------

def hidden() -> dict:
    """Popen/run keyword arguments that stop Windows from flashing a console window."""
    return {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a helper command (netstat, netsh, networksetup...) and capture its text output."""
    kw.setdefault("capture_output", True)
    if IS_WINDOWS:
        kw.setdefault("encoding", "oem")      # Windows console tools print in the OEM code page
        kw.setdefault("errors", "replace")
        kw.setdefault("creationflags", CREATE_NO_WINDOW)
    else:
        kw.setdefault("text", True)
        kw.setdefault("errors", "replace")
    return subprocess.run(cmd, **kw)


def spawn_background(cmd: list[str], log_file, env: dict | None = None) -> subprocess.Popen:
    """Start a long-running process that keeps going after this terminal closes."""
    kw = {"stdout": log_file, "stderr": log_file, "stdin": subprocess.DEVNULL, "env": env}
    if IS_WINDOWS:
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kw["close_fds"] = True
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


# --- processes ------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # Never os.kill(pid, 0) on Windows: it calls TerminateProcess and kills the process.
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # exists, but not ours to open
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by root
        return True


def process_command(pid: int) -> str:
    """What a process runs: its command line on macOS, its executable's path on Windows."""
    if IS_WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return ""
        finally:
            _kernel32.CloseHandle(handle)
    return run(["ps", "-o", "command=", "-p", str(pid)]).stdout


_FILETIME_UNIX_EPOCH = 11_644_473_600  # seconds from 1601-01-01 (where FILETIME counts from) to 1970
_libc = None


def process_started(pid: int) -> float | None:
    """When a process started (Unix time), or None if it isn't running or that can't be read."""
    if pid <= 0:
        return None
    if IS_WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]  # creation, exit, kernel, user
            if not _kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime  # 100 ns steps
            return ticks / 10_000_000 - _FILETIME_UNIX_EPOCH
        finally:
            _kernel32.CloseHandle(handle)
    return _mac_process_started(pid) if IS_MAC else None


def _mac_process_started(pid: int) -> float | None:
    # sysctl(KERN_PROC_PID) fills in a struct kinfo_proc, which starts with the start time
    # (a struct timeval). Works for any process, root's included, and costs one system call.
    global _libc
    import ctypes as c
    import struct
    if _libc is None:
        _libc = c.CDLL(None, use_errno=True)
        _libc.sysctl.argtypes = [c.POINTER(c.c_int), c.c_uint, c.c_void_p, c.POINTER(c.c_size_t),
                                 c.c_void_p, c.c_size_t]
        _libc.sysctl.restype = c.c_int
    mib = (c.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    buf = c.create_string_buffer(1024)  # sizeof(struct kinfo_proc) is 648
    size = c.c_size_t(len(buf))
    if _libc.sysctl(mib, 4, buf, c.byref(size), None, 0) != 0 or size.value < 16:
        return None  # size 0: no such process
    seconds, micros = struct.unpack_from("=qi", buf, 0)
    return seconds + micros / 1_000_000 if seconds > 0 else None


def same_process(pid: int, recorded_at) -> bool:
    """Is `pid` still the process that a record written at `recorded_at` (Unix time) is about?
    PIDs get reused, quickly on Windows and from scratch after every restart, so a live PID
    alone proves nothing: a process that started after the record was written is another one."""
    if not pid_alive(pid):
        return False
    if isinstance(recorded_at, bool) or not isinstance(recorded_at, (int, float)):
        return True  # no time on record (written by an older pogo)
    started = process_started(pid)
    return started is None or started <= recorded_at + 2  # records are in whole seconds


def terminate(pid: int) -> None:
    """Ask a process to stop: SIGTERM on macOS (it cleans up), TerminateProcess on Windows."""
    if IS_WINDOWS:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                _kernel32.TerminateProcess(handle, 1)
            finally:
                _kernel32.CloseHandle(handle)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def kill(pid: int) -> None:
    if IS_WINDOWS:
        terminate(pid)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def process_names() -> set[str]:
    """Lower-case executable names of every running process."""
    if IS_WINDOWS:
        snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == ctypes.c_void_p(-1).value:
            return set()
        names = set()
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while ok:
                names.add(entry.szExeFile.lower())
                ok = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snapshot)
        return names
    return {line.strip().lower() for line in run(["ps", "-axco", "comm="]).stdout.splitlines() if line.strip()}


# --- admin rights ---------------------------------------------------------------------

def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            return bool(_shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False
    return os.geteuid() == 0


def elevate(argv: list[str]) -> int:
    """Windows: start argv with admin rights through a UAC prompt, without a console window.
    Returns a process handle (see wait_process). Raises PermissionError if the user declines."""
    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00008000  # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NO_CONSOLE
    info.lpVerb = "runas"
    info.lpFile = argv[0]
    info.lpParameters = subprocess.list2cmdline(argv[1:])
    info.nShow = 0  # SW_HIDE
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == _ERROR_CANCELLED:
            raise PermissionError("admin permission was declined")
        raise ctypes.WinError(error)
    return info.hProcess


def wait_process(handle: int, timeout: float | None = None) -> int | None:
    """Windows: the exit code of a process started by elevate(), or None if still running."""
    ms = _INFINITE if timeout is None else int(timeout * 1000)
    if _kernel32.WaitForSingleObject(handle, ms) != _WAIT_OBJECT_0:
        return None
    code = wintypes.DWORD()
    _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    return code.value


def close_handle(handle: int) -> None:
    if IS_WINDOWS and handle:
        _kernel32.CloseHandle(handle)


# --- files ------------------------------------------------------------------------------
# Windows refuses to replace or delete a file while another process has it open (pogo's
# local proxy reads the capture state for every new connection), so retry briefly.

def replace_file(src, dst, attempts: int = 50) -> None:
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.05)


def remove_file(path, attempts: int = 50) -> None:
    for attempt in range(attempts):
        try:
            os.unlink(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if not IS_WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(0.05)
