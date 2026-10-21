"""Drive `codex exec` and keep the thread id for `codex exec resume`."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from typing import Any, Callable, Sequence

from orb.backends.base import (
    BackendResult,
    CancelToken,
    CodexCancelled,
    CodexExecError,
    CodexTimeout,
    HighRiskActionBlocked,
)
from orb.codex_safety import assert_argv_safe
from orb.config import AppConfig
from orb.permissions import is_high_risk_command, is_high_risk_tool
from orb.session import SessionStore

log = logging.getLogger("orb.codex")

Runner = Callable[[list[str], str, Callable[[str], None], CancelToken | None, float], tuple[int, str]]

def parse_exec_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _item_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("item_type") or "")


def _item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if isinstance(text, str) and text.strip():
        return text
    message = item.get("message")
    if isinstance(message, str):
        return message
    return ""


def _item_command(item: dict[str, Any]) -> str:
    command = item.get("command") or item.get("cmd") or ""
    return command if isinstance(command, str) else ""


def subprocess_runner(
    argv: list[str],
    stdin_text: str,
    on_line: Callable[[str], None],
    cancel: CancelToken | None,
    timeout: float,
) -> tuple[int, str]:
    """Run Codex, streaming stdout lines. stderr is drained on a side thread."""
    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    stderr_parts: list[str] = []

    def _read_stderr() -> None:
        assert process.stderr is not None
        stderr_parts.append(process.stderr.read())

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()
    state = {"timed_out": False}

    def _kill() -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.kill()

    def _on_timeout() -> None:
        state["timed_out"] = True
        _kill()

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    timer.start()

    def _watch_cancel() -> None:
        if cancel is None:
            return
        while process.poll() is None:
            if cancel.wait(0.1):
                _kill()
                return

    cancel_thread = threading.Thread(target=_watch_cancel, daemon=True)
    cancel_thread.start()
    try:
        if process.stdin is not None:
            if stdin_text:
                process.stdin.write(stdin_text)
            process.stdin.close()
        assert process.stdout is not None
        for line in process.stdout:
            on_line(line)
        return_code = process.wait(timeout=5)
    except Exception:
        _kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        timer.cancel()
        stderr_thread.join(timeout=2)
    if state["timed_out"]:
        raise CodexTimeout(f"codex exec exceeded {timeout}s")
    if cancel is not None and cancel.is_set():
        raise CodexCancelled("codex exec cancelled")
    stderr = stderr_parts[0] if stderr_parts else ""
    return return_code if return_code is not None else 1, stderr


class CodexCliBackend:
    """First backend: local `codex exec` with JSONL events and resume."""

    def __init__(
        self,
        config: AppConfig,
        *,
        runner: Runner | None = None,
        session: SessionStore | None = None,
    ):
        self.config = config
        self.runner = runner or subprocess_runner
        self.session = session if session is not None else SessionStore(config.session.path)

    def always_requires_confirmation(self) -> bool:
        argv = self.build_argv(resume_id=None, use_last=False, images=[], sandbox=self.config.codex.sandbox)
        try:
            assert_argv_safe(
                argv,
                confirmed=False,
                allow_full_auto=self.config.permissions.allow_full_auto,
                allow_danger_full_access=self.config.permissions.allow_danger_full_access,
            )
        except Exception:
            return True
        return False

    def render_prompt(self, user_text: str) -> str:
        preamble = self.config.codex.voice_preamble.strip()
        if not preamble:
            return user_text
        return f"{preamble}\n\n用户说：{user_text}\n"

    def build_argv(
        self,
        *,
        resume_id: str | None,
        use_last: bool,
        images: Sequence[str],
        sandbox: str,
    ) -> list[str]:
        codex = self.config.codex
        argv = [codex.bin, "exec", "--json", "--color", "never"]
        if codex.skip_git_repo_check:
            argv.append("--skip-git-repo-check")
        argv.extend(["-s", sandbox])
        if codex.model:
            argv.extend(["-m", codex.model])
        for override in codex.config_overrides:
            argv.extend(["-c", override])
        if codex.cwd:
            argv.extend(["-C", codex.cwd])
        argv.extend(codex.extra_args)
        if resume_id or use_last:
            argv.append("resume")
            if resume_id:
                argv.append(resume_id)
            elif use_last:
                argv.append("--last")
            for image in images:
                argv.extend(["--image", image])
            argv.append("-")
            return argv
        for image in images:
            argv.extend(["--image", image])
        argv.append("-")
        return argv

    def ask(
        self,
        prompt: str,
        *,
        confirmed: bool = False,
        images: Sequence[str] = (),
        cancel: CancelToken | None = None,
    ) -> BackendResult:
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled before start")
        resume_id, use_last = self._resume_target()
        merged_images = [*self.config.codex.images, *images]
        sandbox = self.config.codex.sandbox
        argv = self.build_argv(resume_id=resume_id, use_last=use_last, images=merged_images, sandbox=sandbox)
        assert_argv_safe(
            argv,
            confirmed=confirmed,
            allow_full_auto=self.config.permissions.allow_full_auto,
            allow_danger_full_access=self.config.permissions.allow_danger_full_access,
        )
        holder: dict[str, Any] = {"thread_id": None, "messages": [], "errors": [], "commands": []}
        log.info(
            "codex exec sandbox=%s resume=%s",
            sandbox,
            resume_id or ("last" if use_last else "new"),
        )
        log.debug("codex argv=%s", argv)

        def on_line(line: str) -> None:
            if cancel is not None and cancel.is_set():
                raise CodexCancelled("cancelled")
            event = parse_exec_line(line)
            if event is None:
                return
            self._consume(event, holder, confirmed=confirmed)

        rendered = self.render_prompt(prompt)
        try:
            return_code, stderr = self.runner(argv, rendered, on_line, cancel, self.config.codex.timeout_s)
        finally:
            thread_id = holder["thread_id"]
            if isinstance(thread_id, str) and thread_id and self.session is not None:
                self.session.save(thread_id, self._cwd())
        messages = holder["messages"]
        text = messages[-1] if messages else ""
        full_text = "\n".join(messages)
        session_id = holder["thread_id"] if isinstance(holder["thread_id"], str) else None
        if holder["errors"]:
            raise CodexExecError("; ".join(holder["errors"]), text=full_text, session_id=session_id)
        if return_code != 0:
            detail = (stderr or "").strip()[-2000:]
            raise CodexExecError(detail or f"codex exec exited {return_code}", text=full_text, session_id=session_id)
        if not messages:
            raise CodexExecError("codex exec returned no agent message", session_id=session_id)
        return BackendResult(
            text=text,
            full_text=full_text,
            session_id=session_id,
            commands=tuple(holder["commands"]),
        )

    def _resume_target(self) -> tuple[str | None, bool]:
        if self.config.codex.resume == "last":
            return None, True
        if self.session is None:
            return None, False
        state = self.session.load()
        if state is None:
            return None, False
        cwd = self._cwd()
        if self.config.session.bind_cwd and state.cwd and cwd:
            if os.path.abspath(state.cwd) != os.path.abspath(cwd):
                log.info("session cwd changed; starting a new Codex thread")
                return None, False
        return state.thread_id, False

    def _cwd(self) -> str:
        if self.config.codex.cwd:
            return self.config.codex.cwd
        return os.getcwd()

    def _consume(self, event: dict[str, Any], holder: dict[str, Any], *, confirmed: bool) -> None:
        kind = str(event.get("type") or "")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                holder["thread_id"] = thread_id
            return
        if kind == "error":
            message = event.get("message") or event.get("error") or "codex error"
            holder["errors"].append(str(message))
            return
        if kind == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                holder["errors"].append(str(error.get("message") or error))
            else:
                holder["errors"].append(str(error or "turn failed"))
            return
        if kind not in {"item.started", "item.updated", "item.completed"}:
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = _item_type(item)
        if item_type in {"agent_message", "assistant_message"} and kind == "item.completed":
            text = _item_text(item).strip()
            if text:
                holder["messages"].append(text)
            return
        if item_type == "command_execution":
            command = _item_command(item)
            if command:
                holder["commands"].append(command)
                if not confirmed and is_high_risk_command(command):
                    raise HighRiskActionBlocked(command)
            return
        if item_type == "file_change" and not confirmed:
            changes = item.get("changes") or []
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict) and str(change.get("kind") or change.get("type") or "") == "delete":
                        path = str(change.get("path") or "file")
                        raise HighRiskActionBlocked(f"delete {path}")
            return
        if item_type == "mcp_tool_call" and not confirmed:
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            if tool and is_high_risk_tool(server, tool):
                raise HighRiskActionBlocked(f"{server} {tool}".strip())

