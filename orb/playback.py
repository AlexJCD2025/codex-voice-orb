"""Play synthesized audio and stop it immediately for barge-in."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading

from orb.config import AppConfig

log = logging.getLogger("orb.playback")


class PlaybackError(RuntimeError):
    """No usable player, or playback failed to start."""


def resolve_player(preference: str) -> list[str]:
    if preference == "auto":
        if shutil.which("afplay"):
            return ["afplay"]
        if sys.platform == "darwin":
            return ["afplay"]
        if shutil.which("aplay"):
            return ["aplay", "-q"]
        if shutil.which("ffplay"):
            return ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
        raise PlaybackError("no audio player found (afplay, aplay, or ffplay)")
    commands = {
        "afplay": ["afplay"],
        "aplay": ["aplay", "-q"],
        "ffplay": ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"],
    }
    if preference not in commands:
        raise PlaybackError(f"unknown player {preference}")
    binary = commands[preference][0]
    if shutil.which(binary) is None and not (sys.platform == "darwin" and binary == "afplay"):
        raise PlaybackError(f"{binary} not found")
    return commands[preference]


class PlaybackController:
    def __init__(self, config: AppConfig, *, command: list[str] | None = None):
        self.config = config
        self._command = command
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._stop_requested = False

    def arm(self) -> None:
        with self._lock:
            self._stop_requested = False

    def play(self, path: str) -> None:
        if not path:
            return
        with self._lock:
            cancelled = self._stop_requested
            self._stop_requested = False
        if cancelled:
            return
        command = list(self._command or resolve_player(self.config.playback.player))
        command.append(path)
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with self._lock:
            if self._stop_requested:
                _terminate(process)
                self._stop_requested = False
                return
            self._process = process
        try:
            process.wait()
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                self._stop_requested = True
                return
            _terminate(process)

    def is_playing(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()


class GatedPlayback:
    """Optionally close the mic while audio plays. Leave this off to allow barge-in."""

    def __init__(self, inner: PlaybackController, audio, enabled: bool):
        self.inner = inner
        self.audio = audio
        self.enabled = enabled

    def arm(self) -> None:
        self.inner.arm()

    def play(self, path: str) -> None:
        if self.enabled:
            self.audio.set_gated(True)
        try:
            self.inner.play(path)
        finally:
            if self.enabled:
                self.audio.set_gated(False)

    def stop(self) -> None:
        self.inner.stop()

    def is_playing(self) -> bool:
        return self.inner.is_playing()
