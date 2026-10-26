"""Stop a child process group without leaving a reader blocked on a pipe."""

from __future__ import annotations

import os
import signal
import subprocess
import time


def close_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:
            pass


def terminate_group(process: subprocess.Popen[str], grace: float) -> None:
    """SIGTERM the process group, then SIGKILL after `grace` seconds."""
    if process.poll() is not None:
        close_pipes(process)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        close_pipes(process)
        return
    except PermissionError:
        try:
            process.kill()
        except Exception:
            pass
        close_pipes(process)
        return
    deadline = time.monotonic() + max(0.0, grace)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                process.kill()
            except Exception:
                pass
    close_pipes(process)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
