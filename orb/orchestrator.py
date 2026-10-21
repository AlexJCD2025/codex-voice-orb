"""One voice turn: read-only plan, confirm concrete actions, then execute."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, TextIO

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
from orb.permissions import classify_confirmation
from orb.plan import (
    PlannedAction,
    PlanResult,
    concrete_actions,
    confirmation_text,
    is_key_only,
    needs_execute_confirmation,
)
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
    actions: tuple[PlannedAction, ...] = ()
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


@dataclass
class _Pending:
    nonce: str
    deadline: float
    turn: _Turn
    key_only: bool
    side_effects: tuple[str, ...] = field(default_factory=tuple)


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
        set_gated: Callable[[bool], None] | None = None,
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
        self.set_gated = set_gated or (lambda _gated: None)
        self.phase = "idle"
        self._lock = threading.Lock()
        self._cancel = CancelToken()
        self._pending: _Pending | None = None

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
                except OSError as exc:
                    log.warning("could not delete utterance %s: %s", wav_path, exc)
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
        if self._pending is not None and key_confirm:
            return self._confirm_with_key()
        if self._pending is not None:
            return self._resolve_voice(text)
        if key_confirm:
            return TurnResult(status="ignored")
        cleaned = text.strip()
        if classify_confirmation(cleaned) == "yes":
            return TurnResult(status="ignored", transcript=cleaned)
        if not is_usable_transcript(cleaned):
            return TurnResult(status="empty", transcript=cleaned)
        turn = self._new_turn(cleaned, "low", stt_ms)
        turn.images = tuple(images)
        if self.config.tts.ack and not self._speak(self.config.tts.ack, turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        return self._run_plan(turn)

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

    def confirmation_remaining(self) -> float | None:
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            return pending.deadline - self.clock()

    def poll(self) -> TurnResult | None:
        with self._lock:
            pending = self._pending
            if pending is None or self.clock() < pending.deadline:
                return None
            self._pending = None
            turn = pending.turn
        self._set_gated(False)
        return self._timeout_turn(turn)

    def _resolve_voice(self, text: str) -> TurnResult:
        decision = classify_confirmation(text)
        with self._lock:
            pending = self._pending
            if pending is None:
                return TurnResult(status="ignored", transcript=text)
            turn = pending.turn
            if self.clock() >= pending.deadline:
                self._pending = None
                action = "expired"
            elif decision == "yes" and pending.key_only:
                return TurnResult(
                    status="awaiting_confirmation",
                    turn_id=turn.turn_id,
                    transcript=turn.transcript,
                    risk=turn.risk,
                )
            elif decision != "yes" and decision != "no" and pending.key_only:
                return TurnResult(
                    status="awaiting_confirmation",
                    turn_id=turn.turn_id,
                    transcript=turn.transcript,
                    risk=turn.risk,
                )
            elif decision == "yes":
                self._pending = None
                action = "execute"
            else:
                self._pending = None
                action = "no" if decision == "no" else "unclear"
        if action == "expired":
            self._set_gated(False)
            return self._timeout_turn(turn)
        if action == "execute":
            return self._run_execute(turn)
        self._set_gated(False)
        if action == "no":
            message = self.config.permissions.cancel_message
        else:
            message = self.config.permissions.unclear_message
        self._emit(message)
        self._speak(message, turn)
        self._finish(turn, "cancelled")
        return TurnResult(status="cancelled", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)

    def _confirm_with_key(self) -> TurnResult:
        with self._lock:
            pending = self._pending
            if pending is None:
                return TurnResult(status="ignored")
            turn = pending.turn
            if self.clock() >= pending.deadline:
                self._pending = None
                expired = True
            else:
                self._pending = None
                expired = False
        self._set_gated(False)
        if expired:
            return self._timeout_turn(turn)
        return self._run_execute(turn)

    def _run_plan(self, turn: _Turn) -> TurnResult:
        with self._lock:
            if self._cancel.is_set():
                already = True
            else:
                already = False
                self.phase = "thinking"
        if already:
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        self._emit("已发给 Codex 做只读规划。")
        started = self.timer()
        try:
            planned = self.backend.plan(turn.transcript, images=turn.images, cancel=self._cancel)
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
            return self._backend_error(turn, started, exc)
        except Exception as exc:
            log.exception("plan failed")
            return self._backend_error(turn, started, exc)
        if not isinstance(planned, PlanResult):
            planned = _coerce_plan(planned)
        turn.codex_ms = _ms(self.timer() - started)
        turn.session_id = planned.session_id
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=planned.text, session_id=planned.session_id)
        actions = concrete_actions(planned.plan, planned.side_effects)
        if not needs_execute_confirmation(planned.plan, planned.side_effects):
            return self._speak_answer(turn, planned.text, planned.session_id)
        turn.actions = actions
        turn.risk = "high"
        key_only = is_key_only(actions, turn.transcript, self.config.codex.sandbox)
        prompt = confirmation_text(actions, planned.side_effects, key_only=key_only)
        nonce = uuid.uuid4().hex
        with self._lock:
            if self._cancel.is_set():
                cancelled = True
            else:
                cancelled = False
                self._pending = _Pending(
                    nonce=nonce,
                    deadline=self.clock() + self.config.permissions.confirm_timeout_s,
                    turn=turn,
                    key_only=key_only,
                    side_effects=planned.side_effects,
                )
                self.phase = "awaiting_confirmation"
        if cancelled:
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=planned.text, session_id=planned.session_id)
        self._set_gated(True)
        self._emit(prompt)
        self._speak(prompt, turn)
        with self._lock:
            pending = self._pending
            if pending is not None and pending.nonce == nonce and not self._cancel.is_set():
                self.phase = "awaiting_confirmation"
        return TurnResult(
            status="awaiting_confirmation",
            turn_id=turn.turn_id,
            transcript=turn.transcript,
            risk=turn.risk,
            session_id=turn.session_id,
        )

    def _run_execute(self, turn: _Turn) -> TurnResult:
        self._set_gated(False)
        turn.confirmed = True
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
        if not self._speak("开始执行。", turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        self._emit("已确认，开始执行。")
        with self._lock:
            self.phase = "thinking"
        started = self.timer()
        try:
            result = self.backend.execute(
                turn.transcript,
                turn.actions,
                images=() if turn.session_id else turn.images,
                resume_id=turn.session_id,
                cancel=self._cancel,
            )
        except CodexCancelled:
            turn.codex_ms = (turn.codex_ms or 0) + _ms(self.timer() - started)
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        except (UnsafeCodexInvocation, CodexExecError, BackendError) as exc:
            return self._backend_error(turn, started, exc, add=True)
        except Exception as exc:
            log.exception("execute failed")
            return self._backend_error(turn, started, exc, add=True)
        turn.codex_ms = (turn.codex_ms or 0) + _ms(self.timer() - started)
        turn.session_id = result.session_id or turn.session_id
        return self._speak_answer(turn, result.text, turn.session_id, full_text=result.full_text)

    def _speak_answer(self, turn: _Turn, text: str, session_id: str | None, full_text: str | None = None) -> TurnResult:
        turn.reply_chars = len(text or "")
        turn.session_id = session_id
        if self._cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=text, session_id=session_id)
        summary = spoken_summary(
            text,
            max_chars=self.config.tts.max_chars,
            max_sentences=self.config.tts.max_sentences,
        )
        self._emit(f"你说：{turn.transcript}")
        self._emit(f"Codex：{full_text if full_text is not None else text}")
        self._emit(f"朗读：{summary}")
        if not self._speak(summary, turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=text, summary=summary, session_id=session_id)
        self._finish(turn, "ok")
        return self._result(turn, "ok", reply=text, summary=summary, session_id=session_id)

    def _backend_error(self, turn: _Turn, started: float, exc: Exception, *, add: bool = False) -> TurnResult:
        elapsed = _ms(self.timer() - started)
        turn.codex_ms = (turn.codex_ms or 0) + elapsed if add else elapsed
        turn.error = str(exc)
        self._emit(f"Codex 执行失败：{exc}")
        self._speak("Codex 执行失败。", turn)
        self._finish(turn, "error")
        return self._result(turn, "error", error=str(exc))

    def _timeout_turn(self, turn: _Turn) -> TurnResult:
        self._emit(self.config.permissions.timeout_message)
        self._speak(self.config.permissions.timeout_message, turn)
        self._finish(turn, "timeout")
        return TurnResult(status="timeout", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)

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
            self._delete_tts(path)
            return False
        with self._lock:
            if self._cancel.is_set():
                self._delete_tts(path)
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
            self._delete_tts(path)
        with self._lock:
            interrupted = self.phase == "interrupted" or self._cancel.is_set()
        return not interrupted

    def _delete_tts(self, path: str) -> None:
        if not path or not self.config.tts.delete_after_playback:
            return
        try:
            os.remove(path)
        except OSError as exc:
            log.warning("could not delete tts audio %s: %s", path, exc)

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

    def _set_gated(self, gated: bool) -> None:
        try:
            self.set_gated(gated)
        except Exception:
            log.warning("mic gate failed", exc_info=True)

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


def _coerce_plan(value: object) -> PlanResult:
    from orb.plan import ExecutionPlan

    text = str(getattr(value, "text", "") or "")
    session_id = getattr(value, "session_id", None)
    return PlanResult(
        plan=ExecutionPlan(spoken=text, execute=False, actions=()),
        session_id=session_id if isinstance(session_id, str) else None,
        raw_text=text,
    )


def _ms(seconds: float) -> int:
    return int(seconds * 1000)
