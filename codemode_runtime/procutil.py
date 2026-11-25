# SPDX-License-Identifier: MIT
"""Processes, on every platform the runtime supports.

Four things differ between Windows and POSIX and are easy to get subtly
wrong: stopping a process together with everything it started, asking
whether a process is still running, starting one that outlives its parent,
and telling whether this process has a terminal. They are written once,
here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional

IS_WINDOWS = os.name == "nt"

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def child_options() -> Dict[str, Any]:
    """``Popen`` keyword arguments for a child this process owns.

    POSIX: its own session, so ``kill_tree`` can signal the whole group and a
    Ctrl-C in the terminal does not reach it behind our back. Windows: no
    console window. A host without a console (a detached worker, a GUI app)
    would otherwise open a visible one for every console child it starts.
    """
    if IS_WINDOWS:
        return {"creationflags": _CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(popen: subprocess.Popen) -> None:
    """Stop a child started with ``child_options`` and everything under it.
    Safe to call on a process that has already exited: what that one left
    running stays, as it does on POSIX."""
    if popen.poll() is not None:
        release(popen)
        return
    try:
        if IS_WINDOWS:
            _end_job(popen)
            # /T takes the whole tree: a command that spawned children must
            # not leave them running after a timeout.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(popen.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, creationflags=_CREATE_NO_WINDOW,
            )
        else:
            os.killpg(popen.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        popen.kill()
    except OSError:
        pass


def contain(popen: subprocess.Popen) -> None:
    """Call right after starting a child with ``child_options``. Windows: put
    it in a job object of its own, so that ``kill_tree`` also reaches a
    process whose parent has already exited. ``taskkill /T`` follows parent
    ids and cannot: a server that runs a build through a short-lived wrapper
    would leave the build running. The job is ended only by ``kill_tree``:
    not when this process exits, and not when the child exits by itself.
    What a child that exited left running (an app a tool opened for the
    user) stays, as it does on POSIX. A process that asks to leave the job
    may leave it, as one that starts a session of its own leaves the group
    on POSIX. Call ``release`` once the child is gone.

    POSIX needs nothing: the session from ``child_options`` holds the whole
    tree. Any failure (an old Windows, a job that allows no nesting) leaves
    the child as it was, and ``kill_tree`` works as it always did."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = _Extended()
        # BREAKAWAY_OK only. Not KILL_ON_JOB_CLOSE: closing the handle, or
        # this process exiting, must not stop anything.
        limits.BasicLimitInformation.LimitFlags = 0x00000800
        process = kernel32.OpenProcess(0x0100 | 0x0001, False, popen.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        placed = False
        if process:
            placed = bool(
                kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))  # extended limits
                and kernel32.AssignProcessToJobObject(job, process)
            )
            kernel32.CloseHandle(process)
        if placed:
            popen._codemode_job = job  # type: ignore[attr-defined]  # ended by kill_tree, closed by release
        else:
            kernel32.CloseHandle(job)
    except (AttributeError, OSError, ValueError):
        pass


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    # An instance of our own: the argument types set here must not change
    # the functions for other users of the shared ctypes.windll.kernel32.
    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    handle = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = handle
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = handle
    kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [handle, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


_JOB_LOCK = threading.Lock()


def _take_job(popen: subprocess.Popen) -> Any:
    """The job handle of a child, taken out so that exactly one caller gets
    it. A watch thread and ``close`` may act on one process at once, and a
    handle closed twice may close an unrelated one that got the same number."""
    with _JOB_LOCK:
        job = getattr(popen, "_codemode_job", None)
        popen._codemode_job = None  # type: ignore[attr-defined]
    return job


def _end_job(popen: subprocess.Popen) -> None:
    """Stop every process in the job ``contain`` put a child in."""
    job = _take_job(popen)
    if not job:
        return
    try:
        kernel32 = _kernel32()
        kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)
    except (AttributeError, OSError):
        pass


def release(popen: subprocess.Popen) -> None:
    """Close the job handle of a child that is gone, so that handles do not
    pile up in a process that starts servers for hours. Nothing is stopped:
    what the child left running stays."""
    job = _take_job(popen)
    if not job:
        return
    try:
        _kernel32().CloseHandle(job)
    except (AttributeError, OSError):
        pass


def stdin_is_terminal() -> bool:
    """Is stdin a terminal that somebody types into?

    ``isatty()`` alone is not the answer on Windows. There it is true for
    every character device, and the null device is one: a command started
    with ``< NUL``, with ``< /dev/null`` in a POSIX-style shell, or by a tool
    that gives its commands no input would pass for a person at a keyboard.
    Windows is asked instead whether the handle is a console. Any doubt is a
    "no": the callers guard approvals.
    """
    try:
        stream = sys.stdin
        if stream is None or not stream.isatty():
            return False
        if not IS_WINDOWS:
            return True
        import ctypes
        import msvcrt

        # An instance of our own: the argument types set here must not change
        # the function for other users of the shared ctypes.windll.kernel32.
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
        kernel32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetConsoleMode.restype = ctypes.c_int
        mode = ctypes.c_ulong()
        return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError):
        return False


def pid_alive(pid: Any) -> bool:
    """Is a process with this id running? A wrong "yes" (the id was reused)
    is possible and harmless here: callers treat "alive" as "do not touch"."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # os.kill(pid, 0) is not a probe on Windows: it terminates the process.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists, it is just not ours
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    """A process that has exited and has not been collected by its parent
    still has an id, and a signal to it still succeeds. Where the system
    says so (Linux), such a process counts as gone: in a container with no
    init process it can stay that way for as long as the container runs."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            stat = handle.read()
    except OSError:
        return False  # no /proc here, or the process is gone: nothing more to learn
    # "pid (name) state ...": the name may hold spaces and brackets, so the
    # state is what follows the last closing bracket.
    return stat.rpartition(b")")[2].split()[:1] == [b"Z"]


def spawn_detached(argv: List[str], log_path: Optional[str] = None, cwd: Optional[str] = None) -> int:
    """Start a process that keeps running after this one exits, with no
    console and no inherited pipes. Returns its pid. Raises OSError.

    Its stdout and stderr go to ``log_path``: a worker that dies at startup
    must leave a reason somewhere.
    """
    sink = open(log_path, "ab") if log_path else open(os.devnull, "wb")
    try:
        options: Dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, cwd=cwd, close_fds=True)
        if not IS_WINDOWS:
            return subprocess.Popen(argv, start_new_session=True, **options).pid
        flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        try:
            # Leave the job object of the launching shell if it lets us:
            # a shell that kills its job on exit would take the worker along.
            return subprocess.Popen(argv, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **options).pid
        except OSError:
            return subprocess.Popen(argv, creationflags=flags, **options).pid
    finally:
        sink.close()
