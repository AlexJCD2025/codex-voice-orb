"""Turn a Codex reply into a short line that can be spoken aloud."""

from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD = re.compile(r"[*_`#>]+")
_SPOKEN = re.compile(r"^(?:摘要|口语|Spoken)\s*[:：]\s*(.+)$", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[。！？!?])")


def strip_markdown(text: str) -> str:
    without_code = _FENCE.sub(" ", text)
    without_links = _LINK.sub(r"\1", without_code)
    plain = _MD.sub("", without_links)
    return re.sub(r"\s+", " ", plain.replace("\n", " ")).strip()


def _trim(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1].rstrip() + "…"


def spoken_summary(text: str, *, max_chars: int = 280, max_sentences: int = 2) -> str:
    raw = text.strip()
    if not raw:
        return ""
    for line in text.splitlines():
        match = _SPOKEN.match(line.strip())
        if match:
            return _trim(strip_markdown(match.group(1)), max_chars)
    cleaned = strip_markdown(raw)
    parts = [part.strip() for part in _SENTENCE.split(cleaned) if part.strip()]
    if not parts:
        return _trim(cleaned, max_chars)
    chosen: list[str] = []
    total = 0
    for part in parts[: max(1, max_sentences)]:
        if chosen and total + len(part) > max_chars:
            break
        chosen.append(part)
        total += len(part)
        if total >= max_chars:
            break
    return _trim("".join(chosen), max_chars)
