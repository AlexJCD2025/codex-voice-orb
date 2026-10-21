"""Coarse risk screen and spoken/key confirmation matching.

The screen is intentionally fail-closed: a keyword hit asks for confirmation
before Codex starts. It does not replace the Codex sandbox.
"""

from __future__ import annotations

import re

_RISK_SOURCES = (
    r"\bdelete\b",
    r"\bdrop\b",
    r"\bdeploy\b",
    r"\bpurchase\b",
    r"\bbuy\b",
    r"\bpay\b",
    r"\bpayment\b",
    r"\bwire\b",
    r"\bmerge\b",
    r"\bforce[- ]push\b",
    r"\bgit\s+push\b",
    r"\brm\s+-",
    r"\bshutdown\b",
    r"\bsend\s+(?:an?\s+)?(?:email|e-mail|message|dm)\b",
    r"\bpost\s+(?:to|on)\b",
    r"\bfull[- ]auto\b",
    r"\byolo\b",
    r"danger-full-access",
    r"删除",
    r"删掉",
    r"清空",
    r"部署",
    r"购买",
    r"付款",
    r"支付",
    r"转账",
    r"发邮件",
    r"发消息",
    r"发帖",
    r"合并",
    r"强制推送",
    r"推送到",
    r"关机",
    r"格式化",
    r"下单",
    r"重置",
)

_COMMAND_SOURCES = (
    r"(?:^|[;&|]\s*)rm\b",
    r"\bgit\s+push\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\b",
    r"\bsudo\b",
    r"\bdrop\s+(?:table|database)\b",
    r"\bdelete\s+from\b",
    r"\bshutdown\b",
    r"\bmkfs\b",
    r"\bdiskutil\b",
    r"curl[^\n|]*\|\s*(?:sh|bash|zsh)\b",
    r"\bchmod\s+-R\s+777\b",
    r"\bdeploy\b",
)

_TOOL_RISK = re.compile(r"delete|remove|drop|deploy|send_|payment|purchase|push", re.IGNORECASE)

_RISK = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _RISK_SOURCES)
_COMMANDS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in _COMMAND_SOURCES)

_YES = {
    "yes",
    "yeah",
    "yep",
    "ok",
    "okay",
    "confirm",
    "confirmed",
    "确认",
    "好",
    "好的",
    "可以",
    "同意",
    "执行",
    "是",
    "对",
    "嗯",
}
_NO = {
    "no",
    "nope",
    "cancel",
    "stop",
    "deny",
    "取消",
    "不用",
    "不要",
    "算了",
    "否",
    "不",
    "别",
}
_PUNCT = re.compile(r"[。！？!?.,，、\s]+")


def classify_risk(text: str) -> str:
    for pattern in _RISK:
        if pattern.search(text):
            return "high"
    return "low"


def is_high_risk_command(command: str) -> bool:
    for pattern in _COMMANDS:
        if pattern.search(command):
            return True
    return False


def is_high_risk_tool(server: str, tool: str) -> bool:
    return _TOOL_RISK.search(f"{server} {tool}") is not None


def normalize_confirmation(text: str) -> str:
    return _PUNCT.sub("", text.strip().lower())


def classify_confirmation(text: str) -> str:
    """Return yes, no, or other. Only an exact short reply counts as yes."""
    normalized = normalize_confirmation(text)
    if normalized in _YES:
        return "yes"
    if normalized in _NO:
        return "no"
    return "other"
