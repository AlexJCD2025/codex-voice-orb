"""Local speech-to-text. faster-whisper is the default; whisper.cpp is optional."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from orb.config import AppConfig

log = logging.getLogger("orb.stt")

_UNUSABLE = {"[silence]", "[blank_audio]", "(silence)", "silence", "blank_audio"}
_WORD = re.compile(r"[\w\u4e00-\u9fff]", re.UNICODE)


class STTError(RuntimeError):
    """Transcription could not run."""


def is_usable_transcript(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped.lower() in _UNUSABLE:
        return False
    return _WORD.search(stripped) is not None


def whisper_cpp_argv(binary: str, model: str, wav_path: str, language: str, output_base: str) -> list[str]:
    lang = language if language and language != "auto" else "auto"
    return [binary, "-m", model, "-f", wav_path, "-l", lang, "-otxt", "-of", output_base, "-np"]


class WhisperSTT:
    def __init__(self, config: AppConfig, *, model=None):
        self.config = config
        self._model = model

    def transcribe(self, wav_path: str) -> str:
        if self.config.stt.engine == "whisper.cpp":
            return self._whisper_cpp(wav_path)
        return self._faster_whisper(wav_path)

    def _faster_whisper(self, wav_path: str) -> str:
        model = self._model or self._load_faster()
        language = None if self.config.stt.language in {"", "auto"} else self.config.stt.language
        segments, _info = model.transcribe(wav_path, language=language, vad_filter=True)
        return "".join(getattr(segment, "text", "") for segment in segments).strip()

    def _load_faster(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise STTError(
                "faster-whisper is not installed. On the Mac run: pip install -e '.[stt]'"
            ) from exc
        model_name = self.config.stt.model_path or self.config.stt.model
        log.info("loading faster-whisper model %s", model_name)
        self._model = WhisperModel(
            model_name,
            device=self.config.stt.device,
            compute_type=self.config.stt.compute_type,
        )
        return self._model

    def _whisper_cpp(self, wav_path: str) -> str:
        model = self.config.stt.model_path or self.config.stt.model
        if not model or not Path(model).expanduser().is_file():
            raise STTError(f"whisper.cpp model file not found: {model}")
        output_base = str(Path(wav_path).with_suffix(""))
        argv = whisper_cpp_argv(
            self.config.stt.whisper_cpp_bin,
            str(Path(model).expanduser()),
            wav_path,
            self.config.stt.language,
            output_base,
        )
        log.info("whisper.cpp %s", " ".join(argv))
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )
        except FileNotFoundError as exc:
            raise STTError(f"whisper.cpp binary not found: {self.config.stt.whisper_cpp_bin}") from exc
        transcript_path = Path(output_base + ".txt")
        if transcript_path.is_file():
            return transcript_path.read_text(encoding="utf-8").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "whisper.cpp failed").strip()
            raise STTError(detail[-2000:])
        return (completed.stdout or "").strip()
