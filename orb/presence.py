"""Filler, progress, and stuck notices while a Codex turn is still running.

Timing follows the long-task idea in xiaozhi-hub-server (MIT, design §6):
say a short filler if the answer is late, then speak less often, and do not
call the turn stuck while a command is still running. The phrases and the
clock below are this project's.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from orb.config import PresenceConfig
from orb.plan import command_is_readonly


@dataclass(frozen=True)
class ActivityView:
    last_event: float
    tools_running: int
    label: str
    started: float


class TurnActivity:
    """What the current foreground turn is doing, updated from JSONL events."""

    def __init__(self, clock):
        self.clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        now = self.clock()
        with self._lock:
            self.started = now
            self.last_event = now
            self.tools = 0
            self.label = ""

    def note(self, label: str, tools_running: int) -> None:
        with self._lock:
            self.last_event = self.clock()
            self.tools = max(0, int(tools_running))
            if label:
                self.label = label

    def view(self) -> ActivityView:
        with self._lock:
            return ActivityView(self.last_event, self.tools, self.label, self.started)


def activity_label(item_type: str, command: str = "") -> str:
    """A few spoken words for one Codex item. Empty means 'no new label'."""
    if item_type == "command_execution":
        if command and command_is_readonly(command):
            return "在看文件"
        return "在跑命令"
    if item_type == "mcp_tool_call":
        return "在调用工具"
    if item_type == "file_change":
        return "在改文件"
    if item_type in {"agent_message", "assistant_message"}:
        return "在整理回答"
    if item_type in {"reasoning", "agent_reasoning"}:
        return "在想"
    return ""


def progress_phrase(label: str, elapsed: float) -> str:
    what = label.strip() if label else ""
    if elapsed >= 55:
        minutes = max(1, int(round(elapsed / 60)))
        if what:
            return f"还在处理，已经{minutes}分钟了，{what}。"
        return f"还在处理，已经{minutes}分钟了。"
    if what:
        return f"还在处理，{what}。"
    return "还在处理，请稍等。"


class Presence:
    """Decide the next short line to say. The caller does the speaking."""

    def __init__(self, config: PresenceConfig, activity: TurnActivity, clock):
        self.config = config
        self.activity = activity
        self.clock = clock
        self.active = False
        self.started = 0.0
        self.next_filler: float | None = None
        self.next_progress: float | None = None
        self.gap = 30.0
        self.stuck_sent = False

    def start(self) -> None:
        now = self.clock()
        self.started = now
        self.active = bool(self.config.enabled)
        self.stuck_sent = False
        self.gap = self.config.progress_start_s if self.config.progress_start_s > 0 else 30.0
        if self.active and self.config.filler and self.config.filler_delay_s >= 0:
            self.next_filler = now + self.config.filler_delay_s
        else:
            self.next_filler = None
        if self.active and self.config.progress_start_s > 0:
            base = self.next_filler if self.next_filler is not None else now
            self.next_progress = base + self.config.progress_start_s
        else:
            self.next_progress = None

    def stop(self) -> None:
        self.active = False
        self.next_filler = None
        self.next_progress = None

    def tick(self, *, quiet: bool) -> str | None:
        """Return one phrase, or None. A busy user or a disabled config says nothing."""
        if not self.active or not self.config.enabled or not quiet:
            return None
        now = self.clock()
        if self.next_filler is not None and now >= self.next_filler:
            self.next_filler = None
            if self.config.progress_start_s > 0:
                self.next_progress = now + self.gap
            return self.config.filler
        if self.next_progress is not None and now >= self.next_progress:
            self.gap = min(self.gap * 2, self.config.progress_max_s)
            self.next_progress = now + self.gap
            return progress_phrase(self.activity.view().label, now - self.started)
        if self._stuck(now):
            self.stuck_sent = True
            return self.config.stuck
        return None

    def _stuck(self, now: float) -> bool:
        if self.config.stuck_after_s <= 0 or not self.config.stuck:
            return False
        view = self.activity.view()
        if view.tools_running > 0:
            return False
        quiet_for = now - view.last_event
        if quiet_for < self.config.stuck_after_s:
            self.stuck_sent = False
            return False
        return not self.stuck_sent
