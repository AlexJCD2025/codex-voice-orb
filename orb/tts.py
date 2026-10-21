"""Text-to-speech. macOS `say` is the default engine; the method is the plugin point."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orb.config import AppConfig
from orb.session import ensure_private_dir


class TTSError(RuntimeError):
    """Synthesis failed."""


class SayTTS:
    """Write an AIFF with `say -o` so playback can be interrupted."""

    def __init__(self, config: AppConfig):
        self.config = config
        self._count = 0

    def synthesize(self, text: str, *, turn_id: str) -> str:
        spoken = text.strip()
        if not spoken:
            return ""
        output_dir = Path(self.config.tts.output_dir)
        ensure_private_dir(output_dir)
        self._count += 1
        path = output_dir / f"{turn_id}-{self._count}.aiff"
        argv = ["say", "-o", str(path)]
        if self.config.tts.voice:
            argv.extend(["-v", self.config.tts.voice])
        if self.config.tts.rate:
            argv.extend(["-r", str(self.config.tts.rate)])
        argv.append(spoken)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise TTSError("macOS say was not found") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "say failed").strip()
            raise TTSError(detail[-1000:])
        if path.is_file():
            os.chmod(path, 0o600)
        return str(path)


class NoTTS:
    """Skip synthesis. The orchestrator still prints the reply."""

    def synthesize(self, text: str, *, turn_id: str) -> str:  # noqa: ARG002
        return ""
