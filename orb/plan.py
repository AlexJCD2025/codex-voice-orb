"""Read-only execution plan returned before any mutating Codex turn.

Shell commands Codex runs while planning are inspection unless they would
change the machine. `pwd`, `rg`, and `sed -n` must not become a confirmation
prompt. A second process still waits for confirmation when the plan asks to
write, delete, pay, deploy, send, or to run outside the read-only sandbox.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from orb.session import ensure_private_dir

KINDS = frozenset({"answer", "write", "delete", "command", "send", "deploy", "pay", "other"})
KEY_ONLY_KINDS = frozenset({"delete", "pay", "deploy", "send"})

_KEY_ONLY_TEXT = re.compile(
    r"\b(?:delete|pay|payment|deploy|send|purchase|shutdown|format|rm)\b"
    r"|删除|删掉|付款|支付|部署|发邮件|发消息|购买|转账|关机|格式化|下单",
    re.IGNORECASE,
)

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["spoken", "execute", "actions"],
    "properties": {
        "spoken": {"type": "string"},
        "execute": {"type": "boolean"},
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "summary", "risk"],
                "properties": {
                    "kind": {"type": "string", "enum": sorted(KINDS)},
                    "summary": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "high"]},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class PlannedAction:
    kind: str
    summary: str
    risk: str


@dataclass(frozen=True)
class ExecutionPlan:
    spoken: str
    execute: bool
    actions: tuple[PlannedAction, ...] = ()


@dataclass(frozen=True)
class PlanResult:
    plan: ExecutionPlan
    session_id: str | None = None
    side_effects: tuple[str, ...] = ()
    raw_text: str = ""

    @property
    def text(self) -> str:
        return self.plan.spoken


def write_plan_schema(directory: Path) -> str:
    ensure_private_dir(directory)
    path = directory / "plan.schema.json"
    payload = json.dumps(PLAN_SCHEMA, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return str(path)


def parse_plan(text: str) -> ExecutionPlan:
    """Parse a plan. Anything that is not a concrete action list fails closed."""
    raw = (text or "").strip()
    if not raw:
        return ExecutionPlan(spoken="", execute=False, actions=())
    candidate = raw
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
        else:
            return ExecutionPlan(spoken=raw, execute=False, actions=())
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return ExecutionPlan(spoken=raw, execute=False, actions=())
    if not isinstance(data, dict):
        return ExecutionPlan(spoken=raw, execute=False, actions=())
    spoken = data.get("spoken")
    if not isinstance(spoken, str) or not spoken.strip():
        spoken = raw
    execute = data.get("execute") is True
    raw_actions = data.get("actions")
    actions: list[PlannedAction] = []
    if isinstance(raw_actions, list):
        for item in raw_actions:
            if not isinstance(item, dict):
                execute = False
                continue
            kind = str(item.get("kind") or "other")
            if kind not in KINDS:
                kind = "other"
            summary = item.get("summary")
            summary_text = summary.strip() if isinstance(summary, str) else ""
            risk = item.get("risk")
            if risk not in {"low", "high"}:
                risk = "high"
            if not summary_text:
                execute = False
                continue
            actions.append(PlannedAction(kind=kind, summary=summary_text, risk=str(risk)))
    else:
        execute = False
    concrete = tuple(action for action in actions if action.kind != "answer")
    if execute and not concrete:
        execute = False
    return ExecutionPlan(spoken=spoken.strip(), execute=execute, actions=tuple(actions))


_READONLY_BIN = frozenset(
    {
        "pwd",
        "ls",
        "rg",
        "grep",
        "egrep",
        "fgrep",
        "ag",
        "ack",
        "fd",
        "cat",
        "head",
        "tail",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "which",
        "whence",
        "whoami",
        "id",
        "uname",
        "date",
        "echo",
        "printf",
        "true",
        "false",
        "basename",
        "dirname",
        "realpath",
        "readlink",
        "cut",
        "sort",
        "uniq",
        "tr",
        "column",
        "nl",
        "od",
        "hexdump",
        "diff",
        "cmp",
        "comm",
        "jq",
        "tree",
        "less",
        "more",
        "bat",
        "printenv",
        "dirname",
    }
)
_GIT_READ = frozenset(
    {"status", "diff", "log", "show", "rev-parse", "ls-files", "describe", "blame", "shortlog", "grep", "version", "help"}
)
_SHELLS = frozenset({"zsh", "bash", "sh"})
_FIND_BAD = frozenset({"-delete", "-exec", "-execdir", "-ok", "-fprint", "-fls"})
_MUTATING = re.compile(
    r"(?:^|[;&|`\n(])\s*(?:(?:/\S+/)?(?:"
    r"rm|mv|cp|mkdir|rmdir|touch|chmod|chown|ln|install|tee|dd|sudo|mkfs|diskutil|"
    r"shutdown|reboot|launchctl|osascript|open|curl|wget|ssh|scp|rsync|npm|pip|pip3|brew|"
    r"python|python3|node|ruby|perl"
    r")\b|git\s+(?:push|commit|reset|clean|checkout|switch|merge|rebase|stash|add|apply|restore|tag)\b)",
    re.IGNORECASE,
)


def unwrap_shell(command: str) -> str:
    """Return the script inside `zsh -lc '...'` / `bash -c`, up to a few layers."""
    text = command.strip()
    for _ in range(3):
        match = re.match(r"^(?:/\S+/)?(?P<shell>zsh|bash|sh)\b(?P<rest>.*)$", text, re.IGNORECASE)
        if not match or match.group("shell").lower() not in _SHELLS:
            break
        rest = match.group("rest").strip()
        if not rest.startswith("-"):
            break
        flag, _, rest = rest.partition(" ")
        if "c" not in flag:
            if rest.startswith("-") and "c" in rest.split(maxsplit=1)[0]:
                _flag, _, rest = rest.partition(" ")
            else:
                break
        rest = rest.strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in {"'", '"'}:
            rest = rest[1:-1]
        if rest == text:
            break
        text = rest.strip()
    return text


def _has_write_redirect(script: str) -> bool:
    quote = None
    for index, char in enumerate(script):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char != ">":
            continue
        previous = script[index - 1] if index else ""
        nxt = script[index + 1] if index + 1 < len(script) else ""
        if previous.isdigit() and nxt == "&":
            continue
        if nxt == "&" and previous != "&":
            continue
        return True
    return False


def _split_segments(script: str) -> list[str] | None:
    if "$(" in script or "`" in script:
        return None
    parts: list[str] = []
    buf: list[str] = []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            buf.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if script.startswith("&&", index) or script.startswith("||", index):
            parts.append("".join(buf))
            buf = []
            index += 2
            continue
        if char in {";", "|", "\n"}:
            parts.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _segment_is_readonly(segment: str) -> bool:
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        return False
    while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
        tokens = tokens[1:]
    while tokens and Path(tokens[0]).name in {"command", "builtin", "time", "nice"}:
        tokens = tokens[1:]
    if not tokens:
        return False
    name = Path(tokens[0]).name
    if name == "git":
        if len(tokens) < 2 or tokens[1] not in _GIT_READ:
            return False
        return "--hard" not in tokens
    if name == "sed":
        return not any(token == "--in-place" or token.startswith("-i") for token in tokens[1:])
    if name == "find":
        return not any(token in _FIND_BAD for token in tokens[1:])
    if name == "env":
        return False
    return name in _READONLY_BIN


def command_is_readonly(command: str) -> bool:
    """True for inspection shells such as pwd, rg, and sed -n. Unknown commands are not reads."""
    script = unwrap_shell(command)
    if not script or _has_write_redirect(script) or command_is_mutating(command):
        return False
    segments = _split_segments(script)
    if not segments:
        return False
    return all(_segment_is_readonly(segment) for segment in segments)


def command_is_mutating(command: str) -> bool:
    """True for shells that would leave a read-only inspection: rm, redirects, git push, sed -i."""
    script = unwrap_shell(command)
    if not script:
        return False
    if _has_write_redirect(script):
        return True
    if _MUTATING.search(script):
        return True
    if re.search(r"(?:^|[\s;&|])sed\b", script, re.IGNORECASE) and re.search(
        r"(?:^|\s)--in-place\b|(?:^|\s)-i(?:\b|\.)", script
    ):
        return True
    if re.search(r"(?:^|[\s;&|])find\b", script) and re.search(r"-(?:delete|exec|execdir|ok|fprint)\b", script):
        return True
    return False


def observation_is_unsettled(item: str) -> bool:
    """A plan-phase event that is not read-only inspection.

    These already happened. They do not by themselves schedule another process,
    and they must not hide the spoken answer.
    """
    if item.startswith("file_change"):
        return True
    if command_is_readonly(item):
        return False
    return True


def action_needs_confirmation(action: PlannedAction, sandbox: str) -> bool:
    """Whether this planned action may start a process outside pure read-only Q&A."""
    if action.kind == "answer":
        return False
    if action.kind in {"write", "delete", "send", "deploy", "pay"}:
        return True
    if command_is_mutating(action.summary) or _KEY_ONLY_TEXT.search(action.summary):
        return True
    if sandbox != "read-only":
        return True
    return False


def actions_requiring_confirmation(plan: ExecutionPlan, sandbox: str) -> tuple[PlannedAction, ...]:
    return tuple(action for action in plan.actions if action_needs_confirmation(action, sandbox))


def needs_execute_confirmation(plan: ExecutionPlan, sandbox: str) -> bool:
    """True when a separate execute process should wait for the user.

    Read-only `command_execution` events are not actions. A keyword in the
    user's sentence is not an action either. Only the plan's own mutating
    actions, or any non-answer action that would run in a wider sandbox, count.
    """
    return bool(actions_requiring_confirmation(plan, sandbox))


def is_key_only(actions: tuple[PlannedAction, ...], user_text: str, sandbox: str) -> bool:
    if sandbox == "danger-full-access":
        return True
    if any(action.kind in KEY_ONLY_KINDS for action in actions):
        return True
    blob = "\n".join([user_text, *(action.summary for action in actions)])
    return _KEY_ONLY_TEXT.search(blob) is not None


def confirmation_text(
    actions: tuple[PlannedAction, ...],
    side_effects: tuple[str, ...],
    *,
    key_only: bool,
) -> str:
    lines: list[str] = []
    unsettled = [item for item in side_effects if observation_is_unsettled(item)]
    if unsettled:
        lines.append("规划阶段已经出现这些非只读事件。它们已经发生，确认与否都不能撤销：")
        lines.extend(unsettled)
    if actions:
        lines.append("确认后才会另起一个执行进程。动作是：")
        for action in actions:
            lines.append(f"{action.kind}：{action.summary}")
    if key_only:
        lines.append("删除、付款、部署或发送只能按确认键。说“确认”不会执行。")
    else:
        lines.append("请说“确认”，或按确认键。")
    return "\n".join(lines)
