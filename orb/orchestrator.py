"""One voice turn: transcribe, confirm if needed, ask Codex, speak a short summary."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TextIO

from orb.backends.base import (
    BackendError,
    CancelToken,
    CodexCancelled,
    CodexExecError,
    HighRiskActionBlocked,
)
from orb.codex_safety import UnsafeCodexInvocation
from orb.config import AppConfig
from orb.metrics import MetricsLog, TurnMetrics
from orb.permissions import classify_confirmation, classify_risk
from orb.stt import is_usable_transcript
from orb.summary import spoken_summary

log = logging.getLogger("orb.orchestrator")


@dataclass
class TurnResult:
    status: str
    turn_id: str = ""
    transcript: str = ""
    reply: str = ""
    summary: str = ""
    risk: str = ""
    session_id: str | None = None
    error: str | None = None


@dataclass
class _Turn:
    turn_id: str
    started: float
    started_at: str
    transcript: str
    risk: str
    images: tuple[str, ...] = ()
    confirmed: bool = False
    interrupted: bool = False
    stt_ms: int | None = None
    permission_ms: int | None = None
    codex_ms: int | None = None
    tts_ms: int | None = None
    playback_ms: int | None = None
    session_id: str | None = None
    reply_chars: int = 0
    error: str | None = None


class Orchestrator:
    def __init__(
        self,
        config: AppConfig,
        backend,
        stt,
        tts,
        playback,
        metrics: MetricsLog,
        *,
        timer=None,
        clock=None,
        stdout: TextIO | None = None,
    ):
        self.config = config
        self.backend = backend
        self.stt = stt
        self.tts = tts
        self.playback = playback
        self.metrics = metrics
        self.timer = timer or time.perf_counter
        self.clock = clock or time.monotonic
        self.stdout = stdout or sys.stdout
        self.phase = "idle"
        self._lock = threading.Lock()
        self._cancel = CancelToken()
        self._pending: _Turn | None = None
        self._pending_deadline: float | None = None

    def handle_wav(self, wav_path: str) -> TurnResult:
        with self._lock:
            self.phase = "transcribing"
            self._cancel = CancelToken()
        started = self.timer()
        try:
            text = self.stt.transcribe(wav_path)
        except Exception as exc:
            log.exception("stt failed")
            turn = self._blank_turn("")
            turn.stt_ms = _ms(self.timer() - started)
            turn.error = str(exc)
            self._finish(turn, "error")
            self._emit(f"转写失败：{exc}")
            return TurnResult(status="error", turn_id=turn.turn_id, error=str(exc))
        finally:
            if self.config.paths.delete_audio_after_stt:
                try:
                    os.remove(wav_path)
                except OSError:
                    pass
        stt_ms = _ms(self.timer() - started)
        if self._cancel.is_set():
            return TurnResult(status="interrupted", transcript=text or "")
        if not is_usable_transcript(text):
            with self._lock:
                self.phase = "idle"
            return TurnResult(status="empty", transcript=text or "", error=None)
        return self.submit_text(text, stt_ms=stt_ms)

    def submit_text(
        self,
        text: str,
        *,
        key_confirm: bool = False,
        stt_ms: int | None = None,
        images: tuple[str, ...] | list[str] = (),
    ) -> TurnResult:
        pending = self._pending
        if pending is not None and key_confirm:
            return self._execute(pending, confirmed=True)
        if pending is not None:
            return self._resolve_voice(text, pending)
        if key_confirm:
            return TurnResult(status="ignored")
        cleaned = text.strip()
        if not is_usable_transcript(cleaned):
            return TurnResult(status="empty", transcript=cleaned)
        risk = "high" if self._needs_confirmation(cleaned) else "low"
        turn = self._new_turn(cleaned, risk, stt_ms)
        turn.images = tuple(images)
        if risk == "high":
            with self._lock:
                self._pending = turn
                self._pending_deadline = self.clock() + self.config.permissions.confirm_timeout_s
                self.phase = "awaiting_confirmation"
            self._emit(self.config.permissions.confirm_prompt)
            self._speak(self.config.permissions.confirm_prompt, turn)
            with self._lock:
                if self._pending is turn and not self._cancel.is_set():
                    self.phase = "awaiting_confirmation"
            return TurnResult(
                status="awaiting_confirmation",
                turn_id=turn.turn_id,
                transcript=cleaned,
                risk=risk,
            )
        return self._execute(turn, confirmed=False)

    def confirm_with_key(self) -> TurnResult:
        return self.submit_text("", key_confirm=True)

    def on_speech_start(self) -> None:
        with self._lock:
            pending = self._pending is not None
            phase = self.phase
        if pending:
            self.playback.stop()
            return
        if phase in {"speaking", "thinking", "transcribing"}:
            self.interrupt()

    def interrupt(self) -> None:
        with self._lock:
            token = self._cancel
            if self.phase in {"speaking", "thinking", "transcribing"}:
                self.phase = "interrupted"
        token.cancel()
        self.playback.stop()

    def poll(self) -> TurnResult | None:
        with self._lock:
            turn = self._pending
            deadline = self._pending_deadline
            if turn is None or deadline is None or self.clock() < deadline:
                return None
            self._pending = None
            self._pending_deadline = None
        self._emit(self.config.permissions.timeout_message)
        self._speak(self.config.permissions.timeout_message, turn)
        self._finish(turn, "timeout")
        return TurnResult(status="timeout", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)

    def _needs_confirmation(self, text: str) -> bool:
        if classify_risk(text) == "high":
            return True
        checker = getattr(self.backend, "always_requires_confirmation", None)
        return bool(checker and checker())

    def _resolve_voice(self, text: str, turn: _Turn) -> TurnResult:
        decision = classify_confirmation(text)
        if decision == "yes":
            return self._execute(turn, confirmed=True)
        self._clear_pending()
        if decision == "no":
            message = self.config.permissions.cancel_message
        else:
            message = self.config.permissions.unclear_message
        self._emit(message)
        self._speak(message, turn)
        self._finish(turn, "cancelled")
        return TurnResult(status="cancelled", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)

    def _execute(self, turn: _Turn, *, confirmed: bool) -> TurnResult:
        self._clear_pending()
        turn.confirmed = confirmed
        turn.permission_ms = max(0, _ms(self.timer() - turn.started) - (turn.stt_ms or 0))
        with self._lock:
            if self._cancel.is_set():
                already = True
            else:
                already = False
                self.phase = "thinking"
        if already:
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if self.config.tts.ack and not self._speak(self.config.tts.ack, turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        self._emit("已发给 Codex，正在等回复。")
        with self._lock:
            self.phase = "thinking"
        started = self.timer()
        try:
            result = self.backend.ask(
                turn.transcript,
                confirmed=confirmed,
                images=turn.images,
                cancel=self._cancel,
            )
        except CodexCancelled:
            turn.codex_ms = _ms(self.timer() - started)
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        except HighRiskActionBlocked as exc:
            turn.codex_ms = _ms(self.timer() - started)
            turn.error = str(exc)
            self._emit(self.config.permissions.blocked_message)
            self._speak(self.config.permissions.blocked_message, turn)
            self._finish(turn, "blocked")
            return self._result(turn, "blocked", error=str(exc))
        except (UnsafeCodexInvocation, CodexExecError, BackendError) as exc:
            turn.codex_ms = _ms(self.timer() - started)
            turn.error = str(exc)
            self._emit(f"Codex 执行失败：{exc}")
            self._speak("Codex 执行失败。", turn)
            self._finish(turn, "error")
            return self._result(turn, "error", error=str(exc))
        except Exception as exc:
            log.exception("backend failed")
            turn.codex_ms = _ms(self.timer() - started)
            turn.error = str(exc)
            self._emit(f"Codex 执行失败：{exc}")
            self._finish(turn, "error")
            return self._result(turn, "error", error=str(exc))
        turn.codex_ms = _ms(self.timer() - started)
        turn.session_id = result.session_id
        turn.reply_chars = len(result.text or "")
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=result.text, session_id=result.session_id)
        summary = spoken_summary(
            result.text,
            max_chars=self.config.tts.max_chars,
            max_sentences=self.config.tts.max_sentences,
        )
        self._emit(f"你说：{turn.transcript}")
        self._emit(f"Codex：{result.full_text}")
        self._emit(f"朗读：{summary}")
        if not self._speak(summary, turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=result.text, summary=summary, session_id=result.session_id)
        self._finish(turn, "ok")
        return self._result(turn, "ok", reply=result.text, summary=summary, session_id=result.session_id)

    def _speak(self, text: str, turn: _Turn) -> bool:
        if not text or self._cancel.is_set():
            return not self._cancel.is_set()
        self.playback.arm()
        started = self.timer()
        path = ""
        try:
            path = self.tts.synthesize(text, turn_id=turn.turn_id)
        except Exception as exc:
            turn.error = f"tts: {exc}"
            self._emit(f"语音合成失败：{exc}")
            log.warning("tts failed: %s", exc)
            return not self._cancel.is_set()
        turn.tts_ms = (turn.tts_ms or 0) + _ms(self.timer() - started)
        if self._cancel.is_set():
            return False
        with self._lock:
            if self._cancel.is_set():
                return False
            self.phase = "speaking"
        play_started = self.timer()
        try:
            if path:
                self.playback.play(path)
        except Exception as exc:
            turn.error = f"playback: {exc}"
            self._emit(f"播放失败：{exc}")
            log.warning("playback failed: %s", exc)
        finally:
            turn.playback_ms = (turn.playback_ms or 0) + _ms(self.timer() - play_started)
        with self._lock:
            interrupted = self.phase == "interrupted" or self._cancel.is_set()
        return not interrupted

    def _new_turn(self, transcript: str, risk: str, stt_ms: int | None) -> _Turn:
        with self._lock:
            self._cancel = CancelToken()
        turn = self._blank_turn(transcript)
        turn.risk = risk
        turn.stt_ms = stt_ms
        return turn

    def _blank_turn(self, transcript: str) -> _Turn:
        return _Turn(
            turn_id=uuid.uuid4().hex[:12],
            started=self.timer(),
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            transcript=transcript,
            risk="low",
        )

    def _clear_pending(self) -> None:
        with self._lock:
            self._pending = None
            self._pending_deadline = None

    def _finish(self, turn: _Turn, status: str) -> None:
        if status == "interrupted":
            turn.interrupted = True
        record = TurnMetrics(
            turn_id=turn.turn_id,
            started_at=turn.started_at,
            status=status,
            risk=turn.risk,
            confirmed=turn.confirmed,
            interrupted=turn.interrupted,
            session_id=turn.session_id,
            transcript_chars=len(turn.transcript),
            reply_chars=turn.reply_chars,
            stt_ms=turn.stt_ms,
            permission_ms=turn.permission_ms,
            codex_ms=turn.codex_ms,
            tts_ms=turn.tts_ms,
            playback_ms=turn.playback_ms,
            total_ms=max(0, _ms(self.timer() - turn.started)),
            error=turn.error,
        )
        self.metrics.write(record)
        with self._lock:
            self.phase = "idle"

    def _result(
        self,
        turn: _Turn,
        status: str,
        *,
        reply: str = "",
        summary: str = "",
        session_id: str | None = None,
        error: str | None = None,
    ) -> TurnResult:
        return TurnResult(
            status=status,
            turn_id=turn.turn_id,
            transcript=turn.transcript,
            reply=reply,
            summary=summary,
            risk=turn.risk,
            session_id=session_id if session_id is not None else turn.session_id,
            error=error if error is not None else turn.error,
        )

    def _emit(self, text: str) -> None:
        print(text, file=self.stdout)
        self.stdout.flush()


def _ms(seconds: float) -> int:
    return int(seconds * 1000)
