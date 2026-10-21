"""Read-only execution plan returned before any mutating Codex turn."""

from __future__ import annotations

import json
import os
import re
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


def concrete_actions(plan: ExecutionPlan, side_effects: tuple[str, ...] = ()) -> tuple[PlannedAction, ...]:
    actions = tuple(action for action in plan.actions if action.kind != "answer")
    if actions:
        return actions
    if side_effects:
        return tuple(
            PlannedAction(kind="other", summary="规划阶段已出现事件：" + item, risk="high") for item in side_effects
        )
    return ()


def needs_execute_confirmation(plan: ExecutionPlan, side_effects: tuple[str, ...] = ()) -> bool:
    """True only when a second, mutating process might be started.

    A high-risk keyword with an empty plan does not count: the keyword screen
    must not launch the configured write sandbox by itself.
    """
    if side_effects:
        return True
    if concrete_actions(plan):
        return True
    return False


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
    if side_effects:
        lines.append("规划阶段已经出现这些事件。日志晚于动作本身，取消不能撤销：")
        lines.extend(side_effects)
    if actions:
        lines.append("确认后才会另起一个执行进程。动作是：")
        for action in actions:
            lines.append(f"{action.kind}：{action.summary}")
    if key_only:
        lines.append("删除、付款、部署或发送只能按确认键。说“确认”不会执行。")
    else:
        lines.append("请说“确认”，或按确认键。")
    return "\n".join(lines)
