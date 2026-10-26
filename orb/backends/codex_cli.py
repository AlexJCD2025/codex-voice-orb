"""Drive `codex exec` as a read-only plan turn, then an optional execute turn."""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from orb.backends.base import (
    BackendResult,
    CancelToken,
    CodexCancelled,
    CodexExecError,
    CodexTimeout,
)
from orb.codex_safety import UnsafeCodexInvocation, assert_argv_safe
from orb.config import AppConfig
from orb.plan import PlannedAction, PlanResult, parse_plan, write_plan_schema
from orb.presence import activity_label
from orb.procutil import terminate_group
from orb.session import SessionStore

log = logging.getLogger("orb.codex")

Runner = Callable[[list[str], str, Callable[[str], None], CancelToken | None, float], tuple[int, str]]


def parse_exec_line(line: str) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        data = json_loads(stripped)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def json_loads(text: str) -> Any:
    import json

    return json.loads(text)


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
    *,
    kill_grace: float = 1.0,
) -> tuple[int, str]:
    """Run Codex. SIGTERM, then SIGKILL, and close pipes so a reader cannot hang."""
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
    lines: queue.Queue[str | None] = queue.Queue()
    stderr_parts: list[str] = []
    state = {"done": False, "timed_out": False, "cancelled": False, "killed": False}
    kill_lock = threading.Lock()
    started = time.monotonic()

    def read_stdout() -> None:
        try:
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                lines.put(line)
        except Exception:
            log.debug("stdout reader ended", exc_info=True)
        finally:
            lines.put(None)

    def read_stderr() -> None:
        try:
            assert process.stderr is not None
            stderr_parts.append(process.stderr.read() or "")
        except Exception:
            log.debug("stderr reader ended", exc_info=True)

    def arm_kill(reason: str) -> None:
        with kill_lock:
            if reason == "timeout":
                state["timed_out"] = True
            else:
                state["cancelled"] = True
            if state["killed"]:
                return
            state["killed"] = True
        terminate_group(process, kill_grace)

    def watch_cancel() -> None:
        if cancel is None:
            return
        while not state["done"] and process.poll() is None:
            if cancel.wait(0.05):
                arm_kill("cancel")
                return

    timer = threading.Timer(timeout, lambda: arm_kill("timeout"))
    timer.daemon = True
    timer.start()
    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    cancel_thread = threading.Thread(target=watch_cancel, daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    cancel_thread.start()
    hard_deadline = started + timeout + kill_grace + 3
    try:
        if process.stdin is not None:
            try:
                if stdin_text:
                    process.stdin.write(stdin_text)
                process.stdin.close()
            except Exception:
                arm_kill("timeout" if state["timed_out"] else "cancel")
                raise
        while True:
            if cancel is not None and cancel.is_set():
                arm_kill("cancel")
            if state["timed_out"]:
                arm_kill("timeout")
            try:
                item = lines.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() > hard_deadline and (state["timed_out"] or state["cancelled"] or process.poll() is not None):
                    break
                continue
            if item is None:
                break
            if state["timed_out"] or state["cancelled"]:
                continue
            on_line(item)
    except Exception:
        if process.poll() is None:
            arm_kill("cancel")
        raise
    finally:
        state["done"] = True
        timer.cancel()
        if process.poll() is None:
            arm_kill("timeout" if state["timed_out"] else "cancel")
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_group(process, 0.2)
    if state["timed_out"]:
        raise CodexTimeout(f"codex exec exceeded {timeout}s")
    if state["cancelled"] or (cancel is not None and cancel.is_set()):
        raise CodexCancelled("codex exec cancelled")
    stderr = stderr_parts[0] if stderr_parts else ""
    return_code = process.returncode if process.returncode is not None else 1
    return return_code, stderr


class CodexCliBackend:
    """Plan with a read-only sandbox, then execute only as a second process."""

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
        return self.config.codex.sandbox == "danger-full-access"

    def plan(
        self,
        prompt: str,
        *,
        images: Sequence[str] = (),
        cancel: CancelToken | None = None,
        on_activity: Callable[[str, int], None] | None = None,
    ) -> PlanResult:
        """Read-only planning turn. JSONL events are recorded, not treated as a gate."""
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled before start")
        self._reject_resume_last()
        resume_id = self._resume_id()
        merged_images = [*self.config.codex.images, *images]
        schema = write_plan_schema(Path(self.config.session.path).expanduser().parent)
        argv = self.build_argv(
            resume_id=resume_id,
            images=merged_images,
            sandbox="read-only",
            output_schema=schema,
            confirmed=False,
        )
        holder = self._holder()
        holder["on_activity"] = on_activity
        log.info("codex plan sandbox=read-only resume=%s", resume_id or "new")
        log.debug("codex argv=%s", argv)
        rendered = self.render_plan_prompt(prompt)
        return_code, stderr = self._invoke(argv, rendered, holder, cancel)
        self._save_if_completed(holder, return_code)
        text = self._final_text(holder)
        full_text = "\n".join(holder["messages"])
        self._raise_for_result(holder, return_code, stderr, text)
        plan = parse_plan(text)
        return PlanResult(
            plan=plan,
            session_id=holder["thread_id"] if isinstance(holder["thread_id"], str) else None,
            side_effects=tuple(holder["side_effects"]),
            raw_text=full_text,
        )

    def execute(
        self,
        prompt: str,
        actions: Sequence[PlannedAction],
        *,
        images: Sequence[str] = (),
        resume_id: str | None = None,
        cancel: CancelToken | None = None,
        on_activity: Callable[[str, int], None] | None = None,
    ) -> BackendResult:
        """Second process. Call this only after the user confirmed the listed actions."""
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled before start")
        self._reject_resume_last()
        if resume_id:
            image_args: list[str] = []
        else:
            image_args = [*self.config.codex.images, *images]
        argv = self.build_argv(
            resume_id=resume_id,
            images=image_args,
            sandbox=self.config.codex.sandbox,
            output_schema=None,
            confirmed=True,
        )
        holder = self._holder()
        holder["on_activity"] = on_activity
        log.info("codex execute sandbox=%s resume=%s", self.config.codex.sandbox, resume_id or "new")
        rendered = self.render_execute_prompt(prompt, actions)
        return_code, stderr = self._invoke(argv, rendered, holder, cancel)
        self._save_if_completed(holder, return_code)
        text = self._final_text(holder)
        self._raise_for_result(holder, return_code, stderr, text)
        if not text:
            raise CodexExecError("codex exec returned no agent message", session_id=self._thread(holder))
        return BackendResult(
            text=text,
            full_text="\n".join(holder["messages"]),
            session_id=self._thread(holder),
            commands=tuple(holder["commands"]),
        )

    def ask(
        self,
        prompt: str,
        *,
        confirmed: bool = False,
        images: Sequence[str] = (),
        cancel: CancelToken | None = None,
    ) -> BackendResult:
        """Compatibility wrapper. Unconfirmed calls are plan-only and never execute."""
        del confirmed
        planned = self.plan(prompt, images=images, cancel=cancel)
        return BackendResult(
            text=planned.text,
            full_text=planned.raw_text,
            session_id=planned.session_id,
            commands=planned.side_effects,
        )

    def render_plan_prompt(self, user_text: str) -> str:
        preamble = self.config.codex.voice_preamble.strip()
        return (
            "规划阶段。这是只读规划，不要修改文件，不要运行会改变系统状态的命令，"
            "不要调用会发送、付款、删除或部署的工具。\n"
            "最终消息必须是 JSON，包含 spoken（给用户朗读的一两句）、execute（布尔）、"
            "actions（数组，每项含 kind、summary、risk）。"
            "kind 只能是 answer、write、delete、command、send、deploy、pay、other。"
            "没有副作用时 execute 为 false。\n\n"
            f"{preamble}\n\n"
            f"用户说：{user_text}\n"
        )

    def render_execute_prompt(self, user_text: str, actions: Sequence[PlannedAction]) -> str:
        lines = ["用户已确认以下动作，只执行这些动作："]
        for action in actions:
            lines.append(f"- {action.kind}: {action.summary}")
        lines.append("")
        lines.append(f"用户说：{user_text}")
        return "\n".join(lines) + "\n"

    def build_argv(
        self,
        *,
        resume_id: str | None,
        images: Sequence[str],
        sandbox: str,
        output_schema: str | None,
        confirmed: bool,
    ) -> list[str]:
        self._reject_resume_last()
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
        if output_schema:
            argv.extend(["--output-schema", output_schema])
        if resume_id:
            argv.extend(["resume", resume_id])
        for image in images:
            argv.extend(["--image", image])
        argv.append("-")
        assert_argv_safe(
            argv,
            confirmed=confirmed,
            allow_full_auto=False,
            allow_danger_full_access=self.config.permissions.allow_danger_full_access,
        )
        return argv

    def _invoke(
        self,
        argv: list[str],
        rendered: str,
        holder: dict[str, Any],
        cancel: CancelToken | None,
    ) -> tuple[int, str]:
        def on_line(line: str) -> None:
            if cancel is not None and cancel.is_set():
                raise CodexCancelled("cancelled")
            event = parse_exec_line(line)
            if event is None:
                return
            self._consume(event, holder)

        return self.runner(argv, rendered, on_line, cancel, self.config.codex.timeout_s)

    def _reject_resume_last(self) -> None:
        if self.config.codex.resume == "last":
            raise UnsafeCodexInvocation("codex.resume last is not allowed; --last is never passed")

    def _resume_id(self) -> str | None:
        if self.session is None:
            return None
        state = self.session.load()
        if state is None:
            return None
        cwd = self._cwd()
        if self.config.session.bind_cwd and state.cwd and cwd:
            if os.path.abspath(state.cwd) != os.path.abspath(cwd):
                log.info("session cwd changed; starting a new Codex thread")
                return None
        return state.thread_id

    def _cwd(self) -> str:
        if self.config.codex.cwd:
            return self.config.codex.cwd
        return os.getcwd()

    def _holder(self) -> dict[str, Any]:
        return {
            "thread_id": None,
            "messages": [],
            "errors": [],
            "commands": [],
            "side_effects": [],
            "seen": set(),
            "active_tools": {},
            "on_activity": None,
            "completed": False,
        }

    def saved_thread_id(self) -> str | None:
        """Thread id already on disk. Used by「继续」. Never reads --last."""
        self._reject_resume_last()
        return self._resume_id()

    def _remember_thread(self, thread_id: str) -> None:
        """Save as soon as Codex reports thread.started, even if this turn is later killed."""
        if self.session is None or not thread_id:
            return
        self.session.save(thread_id, self._cwd())

    def _save_if_completed(self, holder: dict[str, Any], return_code: int) -> None:
        if return_code != 0 or holder["errors"] or not holder["completed"]:
            return
        thread_id = holder["thread_id"]
        if isinstance(thread_id, str) and thread_id and self.session is not None:
            self.session.save(thread_id, self._cwd())

    def _final_text(self, holder: dict[str, Any]) -> str:
        messages = holder["messages"]
        if not messages:
            return ""
        return str(messages[-1])

    def _thread(self, holder: dict[str, Any]) -> str | None:
        thread_id = holder["thread_id"]
        return thread_id if isinstance(thread_id, str) else None

    def _raise_for_result(self, holder: dict[str, Any], return_code: int, stderr: str, text: str) -> None:
        session_id = self._thread(holder)
        if holder["errors"]:
            raise CodexExecError("; ".join(holder["errors"]), text=text, session_id=session_id)
        if return_code != 0:
            detail = (stderr or "").strip()[-2000:]
            raise CodexExecError(detail or f"codex exec exited {return_code}", text=text, session_id=session_id)

    def _consume(self, event: dict[str, Any], holder: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                holder["thread_id"] = thread_id
                # Interrupted and timed-out turns must still be resumable.
                # An auth failure before this event must not overwrite a good id.
                self._remember_thread(thread_id)
            self._poke(holder, "")
            return
        if kind == "turn.started":
            self._poke(holder, "")
            return
        if kind == "turn.completed":
            holder["completed"] = True
            self._poke(holder, "")
            return
        if kind == "error":
            message = event.get("message") or event.get("error") or "codex error"
            holder["errors"].append(str(message))
            self._poke(holder, "")
            return
        if kind == "turn.failed":
            error = event.get("error")
            if isinstance(error, dict):
                holder["errors"].append(str(error.get("message") or error))
            else:
                holder["errors"].append(str(error or "turn failed"))
            self._poke(holder, "")
            return
        if kind not in {"item.started", "item.updated", "item.completed"}:
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = _item_type(item)
        self._track_tool(holder, kind, item, item_type)
        if item_type in {"agent_message", "assistant_message"}:
            self._poke(holder, activity_label(item_type))
            if kind == "item.completed":
                text = _item_text(item).strip()
                if text:
                    holder["messages"].append(text)
            return
        if item_type in {"reasoning", "agent_reasoning"}:
            self._poke(holder, activity_label(item_type))
            return
        # These events arrive after the tool has started. Read-only shells are
        # inspection, not a confirmation. Recording them does not undo them.
        # A still-running command stays in active_tools so silence is not "stuck".
        if item_type == "command_execution":
            command = _item_command(item)
            self._poke(holder, activity_label(item_type, command))
            if not command:
                return
            if not self._mark(holder, ("command", str(item.get("id") or command))):
                return
            holder["commands"].append(command)
            holder["side_effects"].append(command)
            return
        if item_type == "file_change":
            self._poke(holder, activity_label(item_type))
            if not self._mark(holder, ("file", str(item.get("id") or kind))):
                return
            changes = item.get("changes") or []
            if isinstance(changes, list) and changes:
                for change in changes:
                    if isinstance(change, dict):
                        change_kind = str(change.get("kind") or change.get("type") or "change")
                        path = str(change.get("path") or "file")
                        holder["side_effects"].append(f"file_change {change_kind} {path}")
            else:
                holder["side_effects"].append("file_change")
            return
        if item_type == "mcp_tool_call":
            server = str(item.get("server") or "")
            tool = str(item.get("tool") or "")
            label = f"{server} {tool}".strip() or "mcp"
            self._poke(holder, activity_label(item_type))
            if not self._mark(holder, ("mcp", str(item.get("id") or label))):
                return
            holder["side_effects"].append(label)
            return
        self._poke(holder, activity_label(item_type))

    def _track_tool(self, holder: dict[str, Any], kind: str, item: dict[str, Any], item_type: str) -> None:
        if item_type not in {"command_execution", "mcp_tool_call", "file_change"}:
            return
        item_id = str(item.get("id") or "")
        if not item_id:
            return
        active: dict[str, str] = holder["active_tools"]
        status = str(item.get("status") or "")
        if kind == "item.completed" or status in {"completed", "failed", "error"}:
            active.pop(item_id, None)
            return
        if kind == "item.started" or status in {"in_progress", "running"}:
            active[item_id] = item_type

    def _poke(self, holder: dict[str, Any], label: str) -> None:
        callback = holder.get("on_activity")
        if callback is None:
            return
        try:
            callback(label, len(holder["active_tools"]))
        except Exception:
            log.debug("activity callback failed", exc_info=True)

    def _mark(self, holder: dict[str, Any], key: tuple[str, str]) -> bool:
        seen: set[tuple[str, str]] = holder["seen"]
        if key in seen:
            return False
        seen.add(key)
        return True

