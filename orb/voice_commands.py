"""Short voice commands for a turn that is already running.

The utterance has to be the command, not a longer question that happens to
contain the word. Resume keeps any question that follows it. Which phrases
matter is adapted from xiaozhi-hub-server; this matcher is not that code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PUNCT = re.compile(r"[\s，。！？、,.!?；;：:~～\"'“”‘’…]+")
_LEADING = re.compile(r"^(?:嗯|啊|呃|哦|喂|那|好的|好|那么|我们|咱们|帮我|请|麻烦)+")
_TRAILING = re.compile(r"(?:吧|啊|呀|呢|啦|嘛|吗|好吗|行吗|可以吗)+$")

STOP = ("别做了", "不要做了", "不要了", "停下", "停止", "取消", "cancel", "stop", "停")
BACKGROUND = ("放到后台", "放后台", "后台继续", "后台做", "你先做着", "先这样")
RESUME = (
    "接着上次聊",
    "继续上次的话题",
    "继续刚才的话题",
    "接着刚才聊",
    "继续上次",
    "继续",
)

RESUME_PROMPT = "请接着刚才的话题，用一两句口语说明现在的情况。"


@dataclass(frozen=True)
class VoiceCommand:
    kind: str
    remainder: str = ""


def normalize(text: str) -> str:
    return _PUNCT.sub("", text or "").strip().lower()


def match_voice_command(text: str) -> VoiceCommand | None:
    """Return stop, background, or resume. None means this is a normal request."""
    raw = normalize(text)
    if not raw:
        return None
    stripped = _TRAILING.sub("", _LEADING.sub("", raw))
    cores: list[str] = []
    for core in (raw, stripped):
        if core and core not in cores:
            cores.append(core)
    for core in cores:
        found = _match_core(core)
        if found is not None:
            return found
    return None


def _match_core(core: str) -> VoiceCommand | None:
    for key in STOP:
        if core == key:
            return VoiceCommand("stop")
    for key in BACKGROUND:
        if core == key:
            return VoiceCommand("background")
    for key in RESUME:
        if core == key:
            return VoiceCommand("resume", "")
        if core.startswith(key):
            return VoiceCommand("resume", core[len(key) :])
    return None
