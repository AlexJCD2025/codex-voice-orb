"""Microphone capture, push-to-talk, and an energy VAD."""

from __future__ import annotations

import logging
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from orb.config import AppConfig

log = logging.getLogger("orb.audio")


class AudioDependencyError(RuntimeError):
    """sounddevice is missing or the input device could not be opened."""


@dataclass(frozen=True)
class SpeechEvent:
    kind: str
    monotonic_ns: int
    wav_path: str | None = None


def rms_pcm16(pcm: bytes) -> float:
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    total = 0
    for index in range(count):
        sample = int.from_bytes(pcm[index * 2 : index * 2 + 2], "little", signed=True)
        total += sample * sample
    return (total / count) ** 0.5 / 32768.0


def floats_to_pcm16(frames: object) -> bytes:
    try:
        import numpy as np
    except ImportError:
        return _floats_to_pcm16_pure(frames)
    array = np.asarray(frames, dtype="float32")
    if array.ndim > 1:
        array = array[:, 0]
    array = np.clip(array, -1.0, 1.0)
    return (array * 32767.0).astype("<i2").tobytes()


def _floats_to_pcm16_pure(frames: object) -> bytes:
    values: list[float] = []
    if isinstance(frames, (bytes, bytearray)):
        raise TypeError("expected float samples, got bytes")
    for item in frames:  # type: ignore[union-attr]
        if isinstance(item, (list, tuple)):
            values.append(float(item[0]) if item else 0.0)
        else:
            values.append(float(item))
    out = bytearray()
    for sample in values:
        bounded = max(-1.0, min(1.0, sample))
        out += int(bounded * 32767).to_bytes(2, "little", signed=True)
    return bytes(out)


def write_wav(path: Path, pcm: bytes, sample_rate: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return str(path)


class RMSVAD:
    """Start speech after enough loud audio and end it after trailing silence."""

    def __init__(self, sample_rate: int, threshold: float, start_ms: int, end_ms: int):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self.start_ms = start_ms
        self.end_ms = end_ms
        self._in_speech = False
        self._voiced_ms = 0.0
        self._silence_ms = 0.0
        self._audio = bytearray()
        self._preroll = bytearray()
        self._preroll_max = int(sample_rate * 0.3) * 2

    def feed(self, pcm: bytes) -> list[str]:
        count = len(pcm) // 2
        if count == 0:
            return []
        pcm = pcm[: count * 2]
        duration_ms = count / self.sample_rate * 1000
        level = rms_pcm16(pcm)
        events: list[str] = []
        if not self._in_speech:
            self._preroll.extend(pcm)
            if len(self._preroll) > self._preroll_max:
                self._preroll = self._preroll[-self._preroll_max :]
            if level >= self.threshold:
                self._voiced_ms += duration_ms
            else:
                self._voiced_ms = 0.0
            if self._voiced_ms >= self.start_ms:
                self._in_speech = True
                self._silence_ms = 0.0
                self._audio = bytearray(self._preroll)
                events.append("start")
            return events
        self._audio.extend(pcm)
        if level >= self.threshold:
            self._silence_ms = 0.0
        else:
            self._silence_ms += duration_ms
        if self._silence_ms >= self.end_ms:
            self._in_speech = False
            self._voiced_ms = 0.0
            events.append("end")
        return events

    def take_audio(self) -> bytes:
        data = bytes(self._audio)
        self._audio = bytearray()
        self._preroll = bytearray()
        return data


class AudioInput:
    """Capture mic audio and emit speech_start / speech_end events."""

    def __init__(self, config: AppConfig, on_event, *, opener=None):
        self.config = config
        self.on_event = on_event
        self._opener = opener
        self._gated = False
        self._held = False
        self._buffer = bytearray()
        self._stream = None
        vad = config.audio.vad
        self._vad = RMSVAD(
            sample_rate=config.audio.sample_rate,
            threshold=vad.threshold,
            start_ms=vad.start_ms,
            end_ms=vad.end_ms,
        )

    def start(self) -> None:
        if self._opener is not None:
            self._stream = self._opener(self.on_pcm)
            return
        self._stream = open_sounddevice_stream(self.config, self.on_pcm)

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        stop = getattr(stream, "stop", None)
        close = getattr(stream, "close", None)
        if callable(stop):
            stop()
        if callable(close):
            close()

    def set_gated(self, gated: bool) -> None:
        self._gated = gated

    def set_ptt(self, held: bool) -> None:
        if self.config.audio.mode != "ptt":
            return
        if held and not self._held:
            self._held = True
            self._buffer = bytearray()
            self._emit("speech_start", None)
            return
        if not held and self._held:
            self._held = False
            self._finish_ptt()

    def on_pcm(self, pcm: bytes) -> None:
        if self._gated or not pcm:
            return
        if self.config.audio.mode == "ptt":
            if self._held:
                self._buffer.extend(pcm)
            return
        for kind in self._vad.feed(pcm):
            if kind == "start":
                self._emit("speech_start", None)
            elif kind == "end":
                self._emit("speech_end", self._write(self._vad.take_audio()))

    def _finish_ptt(self) -> None:
        pcm = bytes(self._buffer)
        self._buffer = bytearray()
        duration_ms = (len(pcm) // 2) / self.config.audio.sample_rate * 1000
        if duration_ms < self.config.audio.min_utterance_ms:
            log.info("discarded %.0f ms push-to-talk clip", duration_ms)
            return
        self._emit("speech_end", self._write(pcm))

    def _write(self, pcm: bytes) -> str:
        directory = Path(self.config.paths.audio_dir)
        path = directory / f"{uuid.uuid4().hex}.wav"
        return write_wav(path, pcm, self.config.audio.sample_rate)

    def _emit(self, kind: str, wav_path: str | None) -> None:
        self.on_event(SpeechEvent(kind=kind, monotonic_ns=time.monotonic_ns(), wav_path=wav_path))


def open_sounddevice_stream(config: AppConfig, on_pcm):
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioDependencyError(
            "sounddevice is not installed. On the Mac run: pip install -e '.[audio]'"
        ) from exc

    def callback(indata, frames, time_info, status) -> None:  # noqa: ARG001
        if status:
            log.warning("audio status: %s", status)
        try:
            on_pcm(floats_to_pcm16(indata))
        except Exception:
            log.exception("audio callback failed")

    try:
        stream = sd.InputStream(
            samplerate=config.audio.sample_rate,
            channels=1,
            dtype="float32",
            device=config.audio.input_device,
            callback=callback,
            blocksize=max(1, int(config.audio.sample_rate * 0.02)),
        )
        stream.start()
    except Exception as exc:
        raise AudioDependencyError(f"could not open microphone: {exc}") from exc
    return stream


def list_input_devices() -> list[dict[str, object]]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioDependencyError(
            "sounddevice is not installed. On the Mac run: pip install -e '.[audio]'"
        ) from exc
    devices = sd.query_devices()
    default_input = None
    try:
        default_input = sd.default.device[0]
    except Exception:
        default_input = None
    found: list[dict[str, object]] = []
    for index, device in enumerate(devices):
        channels = int(device.get("max_input_channels") or 0)
        if channels <= 0:
            continue
        found.append(
            {
                "index": index,
                "name": str(device.get("name") or ""),
                "channels": channels,
                "default": index == default_input,
            }
        )
    return found
