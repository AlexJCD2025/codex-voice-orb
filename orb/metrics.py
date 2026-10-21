"""Per-turn latency records. The log stores timings, not transcripts."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("orb.metrics")


@dataclass
class TurnMetrics:
    turn_id: str
    started_at: str
    status: str
    risk: str
    confirmed: bool
    interrupted: bool
    session_id: str | None
    transcript_chars: int
    reply_chars: int
    stt_ms: int | None = None
    permission_ms: int | None = None
    codex_ms: int | None = None
    tts_ms: int | None = None
    playback_ms: int | None = None
    total_ms: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MetricsLog:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def write(self, metrics: TurnMetrics) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(metrics.to_dict(), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        log.info(
            "turn=%s status=%s risk=%s confirmed=%s stt_ms=%s permission_ms=%s codex_ms=%s tts_ms=%s playback_ms=%s total_ms=%s",
            metrics.turn_id,
            metrics.status,
            metrics.risk,
            metrics.confirmed,
            metrics.stt_ms,
            metrics.permission_ms,
            metrics.codex_ms,
            metrics.tts_ms,
            metrics.playback_ms,
            metrics.total_ms,
        )
