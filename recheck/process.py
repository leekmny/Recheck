"""Bounded asynchronous trusted-tool processes. Targets are never run here directly."""

from __future__ import annotations
import asyncio
import base64
import inspect
import os
from pathlib import Path
import signal
import subprocess
import time

from .paths import plain_local_path


class _WindowsJob:
    """Own trusted-tool descendants, including after their parent has exited."""

    def __init__(self, memory_limit=None):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t),
                ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "read_operations",
                    "write_operations",
                    "other_operations",
                    "read_bytes",
                    "write_bytes",
                    "other_bytes",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", IoCounters),
                ("process_memory", ctypes.c_size_t),
                ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t),
                ("peak_job_memory", ctypes.c_size_t),
            ]

        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, returns in [
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            (
                "SetInformationJobObject",
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                wintypes.BOOL,
            ),
            ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("OpenThread", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            ("ResumeThread", [wintypes.HANDLE], wintypes.DWORD),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ]:
            function = getattr(self.api, name)
            function.argtypes, function.restype = args, returns
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            limits.basic.flags |= 0x0200  # JOB_OBJECT_LIMIT_JOB_MEMORY
            limits.job_memory = memory_limit
        if not self.api.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, pid):
        # CREATE_SUSPENDED prevents children from escaping before job assignment.
        # ResumeThread is a documented API; no host process outside this job is touched.
        import psutil

        process = self.api.OpenProcess(0x0101, False, pid)  # SET_QUOTA | TERMINATE
        if not process:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, process):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            threads = psutil.Process(pid).threads()
            if not threads:
                raise RuntimeError("도구 프로세스의 시작 스레드를 찾을 수 없습니다.")
            for info in threads:
                thread = self.api.OpenThread(0x0002, False, info.id)  # SUSPEND_RESUME
                if not thread:
                    raise self.ctypes.WinError(self.ctypes.get_last_error())
                try:
                    if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                        raise self.ctypes.WinError(self.ctypes.get_last_error())
                finally:
                    self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(process)

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


async def run_process(
    argv,
    *,
    stdin=b"",
    timeout=30,
    cancel=None,
    output_limit=1048576,
    cwd=None,
    env=None,
    on_output=None,
    on_start=None,
    memory_limit=None,
    kill_descendants=False,
    keep_stdin_open=False,
):
    argv = [str(a) for a in argv]
    if not argv or any("\0" in a for a in argv):
        raise ValueError("Invalid process arguments")
    if timeout <= 0 or not 1 <= output_limit <= 64 * 1024 * 1024:
        raise ValueError("Invalid process limits")
    # Windows batch launchers interpret shell metacharacters even with shell=False.
    # Refuse those characters instead of ever interpolating an untrusted command.
    if os.name == "nt" and Path(argv[0]).suffix.lower() in {".bat", ".cmd"}:
        if any(any(c in a for c in '&|<>^%!\r\n"') for a in argv):
            raise ValueError(
                "Ghidra batch launcher path contains shell metacharacters; use a simple path"
            )
        # cmd.exe does not understand the extended local filesystem namespace.
        # Unwrap only the executable/cwd boundary, never arbitrary argument text.
        launcher = str(plain_local_path(argv[0]))
        if len(launcher.encode('utf-16-le')) // 2 >= 260:
            raise ValueError('배치 실행 도구는 짧은 경로에 설치해 주세요. cmd.exe 경로 제한을 초과했습니다.')
        argv[0] = launcher
        if cwd is not None:
            cwd = str(plain_local_path(cwd))
            if len(cwd.encode('utf-16-le')) // 2 >= 260:
                raise ValueError('배치 실행 도구의 작업 폴더를 짧은 경로로 지정해 주세요.')
        # CreateProcess launches .bat/.cmd through Windows itself. Prepending cmd
        # would double-escape quotes and break paths with spaces. Every argument
        # above has been checked against cmd expansion/metacharacters.
    started = time.monotonic()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    overflow = asyncio.Event()
    result = dict(
        argv=argv,
        returncode=None,
        cancelled=False,
        timed_out=False,
        truncated=False,
        error=None,
        cleanup_errors=[],
    )
    if cancel is not None and cancel.is_set():
        result["cancelled"] = True
        result.update(stdout="", stderr="", stdout_b64="", stderr_b64="", duration=0)
        return result
    options = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004
        )  # CREATE_SUSPENDED
    else:
        options["start_new_session"] = True
    proc = None
    job = None
    readers = []

    async def drain(stream, key):
        nonlocal total
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break
            available = max(0, output_limit - total)
            buffers[key].extend(chunk[:available])
            total += min(len(chunk), available)
            if len(chunk) > available:
                overflow.set()
            if on_output and available:
                value = on_output(key, chunk[:available])
                if inspect.isawaitable(value):
                    await value

    async def feed():
        try:
            proc.stdin.write(stdin if isinstance(stdin, bytes) else stdin.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if not keep_stdin_open:
                proc.stdin.close()

    async def terminate():
        if job:
            try:
                job.terminate()
            except Exception as exc:
                result['cleanup_errors'].append(str(exc))
        elif os.name == "nt" and proc.returncode is None:
            taskkill = str(
                Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"
            )
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        elif os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            result['cleanup_errors'].append('도구 종료 후 프로세스 대기 제한 시간을 초과했습니다.')

    async def finish_readers():
        if not readers:
            return
        _, pending = await asyncio.wait(readers, timeout=1)
        if pending:
            result['cleanup_errors'].append(
                '도구 종료 후 출력 파이프가 닫히지 않아 수집 작업을 중단했습니다.'
            )
            for task in pending:
                task.cancel()
            if proc and getattr(proc, '_transport', None):
                proc._transport.close()
        await asyncio.gather(*readers, return_exceptions=True)

    try:
        if os.name == "nt":
            job = _WindowsJob() if memory_limit is None else _WindowsJob(memory_limit)
        proc = await asyncio.create_subprocess_exec(*argv, **options)
        if job:
            job.assign_and_resume(proc.pid)
        if on_start is not None:
            on_start(proc.pid)
        readers = [
            asyncio.create_task(drain(proc.stdout, "stdout")),
            asyncio.create_task(drain(proc.stderr, "stderr")),
            asyncio.create_task(feed()),
        ]
        # A completed parent is insufficient: descendants may still own its pipes.
        # Keep the deadline and cancellation active until all streams have closed.
        while proc.returncode is None or not all(task.done() for task in readers):
            for task in readers:
                if task.done() and not task.cancelled() and task.exception() is not None:
                    raise task.exception()
            if cancel is not None and cancel.is_set():
                result["cancelled"] = True
                await terminate()
                break
            if overflow.is_set():
                result["truncated"] = True
                await terminate()
                break
            if time.monotonic() - started >= timeout:
                result["timed_out"] = True
                await terminate()
                break
            await asyncio.sleep(0.03)
        await finish_readers()
        for task in readers:
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()
        result["returncode"] = proc.returncode
        result["truncated"] = result["truncated"] or overflow.is_set()
    except asyncio.CancelledError:
        result["cancelled"] = True
        if proc:
            await terminate()
        if readers:
            await finish_readers()
        raise
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if proc:
            await terminate()
        if readers:
            await finish_readers()
    finally:
        if proc and keep_stdin_open:
            proc.stdin.close()
        if job:
            job.close()
        elif kill_descendants and proc and os.name != "nt":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    result["duration"] = round(time.monotonic() - started, 4)
    for key, value in buffers.items():
        result[key] = bytes(value).decode("utf-8", "replace")
        result[key + "_b64"] = base64.b64encode(value).decode("ascii")
    return result
