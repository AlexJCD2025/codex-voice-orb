"""One voice turn: read-only plan, confirm concrete actions, then execute.

Long turns stay audible. A filler and later progress lines are spoken while
Codex is still working, and they stop when the user talks. 「后台做」keeps the
process alive and reports at the next quiet moment. 「停」still kills it.
Neither path skips the confirmation gate.
"""

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
    CodexTimeout,
    HighRiskActionBlocked,
)
from orb.codex_safety import UnsafeCodexInvocation
from orb.config import AppConfig
from orb.metrics import MetricsLog, TurnMetrics
from orb.permissions import classify_confirmation
from orb.plan import (
    PlannedAction,
    PlanResult,
    actions_requiring_confirmation,
    confirmation_text,
    is_key_only,
    observation_is_unsettled,
)
from orb.presence import Presence, TurnActivity
from orb.stt import is_usable_transcript
from orb.summary import spoken_summary
from orb.voice_commands import RESUME_PROMPT, match_voice_command

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


@dataclass
class _Job:
    kind: str
    turn: _Turn
    cancel: CancelToken
    t0: float
    done: threading.Event = field(default_factory=threading.Event)
    outcome: object = None
    error: BaseException | None = None
    detached: bool = False
    suppress: bool = False
    finalized: bool = False
    thread: threading.Thread | None = None


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
        self._speech_cancel = CancelToken()
        self._pending: _Pending | None = None
        self._foreground: _Job | None = None
        self._background: _Job | None = None
        self._graveyard: list[_Job] = []
        self._user_speaking = False
        self.activity = TurnActivity(self.clock)
        self.presence = Presence(config.presence, self.activity, self.clock)

    def handle_wav(self, wav_path: str, *, wait: bool = True) -> TurnResult:
        running = self._foreground_running() is not None
        if not running:
            with self._lock:
                self.phase = "transcribing"
                self._speech_cancel = CancelToken()
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
        if not running and self._speech_cancel.is_set():
            return TurnResult(status="interrupted", transcript=text or "")
        if not is_usable_transcript(text):
            self._user_speaking = False
            if not running:
                with self._lock:
                    self.phase = "idle"
            return TurnResult(status="empty", transcript=text or "", error=None)
        return self.submit_text(text, stt_ms=stt_ms, wait=wait)

    def submit_text(
        self,
        text: str,
        *,
        key_confirm: bool = False,
        stt_ms: int | None = None,
        images: tuple[str, ...] | list[str] = (),
        wait: bool = True,
    ) -> TurnResult:
        self._user_speaking = False
        self._reap()
        had_pending = self._pending is not None
        ready = None if had_pending else self._collect_foreground()
        if self._foreground_running() is not None:
            return self._steer(text, key_confirm=key_confirm, wait=wait)
        if self._pending is not None and key_confirm:
            return self._confirm_with_key(wait=wait)
        if self._pending is not None and had_pending:
            return self._resolve_voice(text, wait=wait)
        if self._pending is not None:
            return ready or TurnResult(status="awaiting_confirmation")
        if key_confirm:
            if ready is not None:
                return ready
            return TurnResult(status="ignored")
        command = match_voice_command(text)
        if command is not None and command.kind == "stop":
            if self._background_running() is not None or self._report_waiting():
                return self._stop_background()
            self._say_idle("现在没有在做的事。")
            return TurnResult(status="ignored", transcript=text)
        if command is not None and command.kind == "background":
            self._say_idle("现在没有在做的事。")
            return TurnResult(status="ignored", transcript=text)
        if command is not None and command.kind == "resume":
            return self._resume(command.remainder, stt_ms=stt_ms, images=images, wait=wait)
        cleaned = text.strip()
        if ready is not None and not cleaned:
            return ready
        if classify_confirmation(cleaned) == "yes":
            return TurnResult(status="ignored", transcript=cleaned)
        if not is_usable_transcript(cleaned):
            if ready is not None:
                return ready
            return TurnResult(status="empty", transcript=cleaned)
        return self._begin(cleaned, stt_ms=stt_ms, images=images, wait=wait)

    def confirm_with_key(self, *, wait: bool = True) -> TurnResult:
        return self.submit_text("", key_confirm=True, wait=wait)

    def on_speech_start(self) -> None:
        """The user started talking. Stop our audio. Do not kill a running Codex turn."""
        self._user_speaking = True
        self.playback.stop()
        with self._lock:
            pending = self._pending is not None
            job = self._foreground
        if pending:
            return
        if job is not None and not job.done.is_set() and not job.detached and not job.finalized and not job.suppress:
            return
        self.interrupt()

    def interrupt(self) -> None:
        with self._lock:
            token = self._speech_cancel
            if self.phase in {"speaking", "thinking", "transcribing"}:
                self.phase = "interrupted"
        token.cancel()
        self.playback.stop()

    def shutdown(self) -> None:
        self._user_speaking = False
        jobs = [self._foreground, self._background, *self._graveyard]
        for job in jobs:
            if job is not None and not job.done.is_set():
                job.suppress = True
                job.cancel.cancel()
        self.playback.stop()

    def is_running(self) -> bool:
        """True while the foreground Codex process has not finished."""
        return self._foreground_running() is not None

    def needs_poll(self) -> bool:
        if self._pending is not None:
            return True
        if self._foreground is not None and not self._foreground.finalized:
            return True
        if self._background is not None and not self._background.finalized:
            return True
        return bool(self._graveyard)

    def confirmation_remaining(self) -> float | None:
        with self._lock:
            pending = self._pending
            if pending is None:
                return None
            return pending.deadline - self.clock()

    def poll(self) -> TurnResult | None:
        self._reap()
        self._tick_presence()
        current = self._collect_foreground()
        if current is not None:
            return current
        expired = self._expire_confirmation()
        if expired is not None:
            return expired
        return self._deliver_background()

    def _resume(self, remainder: str, *, stt_ms, images, wait: bool) -> TurnResult:
        if not self._saved_thread():
            self._say_idle("没有上次的对话可以接。")
            return TurnResult(status="ignored", transcript=remainder or "继续")
        text = remainder.strip() or RESUME_PROMPT
        return self._begin(text, stt_ms=stt_ms, images=images, wait=wait)

    def _saved_thread(self) -> str | None:
        fn = getattr(self.backend, "saved_thread_id", None)
        if not callable(fn):
            return None
        try:
            value = fn()
        except Exception:
            log.warning("saved thread lookup failed", exc_info=True)
            return None
        if isinstance(value, str) and value:
            return value
        return None

    def _begin(self, text: str, *, stt_ms, images, wait: bool) -> TurnResult:
        turn = self._new_turn(text, "low", stt_ms)
        turn.images = tuple(images)
        if self.config.tts.ack and not self._speak(self.config.tts.ack, turn):
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if self._speech_cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        return self._start_plan(turn, wait=wait)

    def _start_plan(self, turn: _Turn, *, wait: bool) -> TurnResult:
        self._emit("已发给 Codex 做只读规划。")
        job = self._open_job(turn, "plan")

        def fn():
            return self.backend.plan(
                turn.transcript,
                images=turn.images,
                cancel=job.cancel,
                on_activity=lambda label, tools, bound=job: self._note(bound, label, tools),
            )

        self._launch(job, fn)
        if not wait:
            return TurnResult(status="running", turn_id=turn.turn_id, transcript=turn.transcript)
        return self._wait_job(job)

    def _steer(self, text: str, *, key_confirm: bool, wait: bool) -> TurnResult:
        job = self._foreground_running()
        if job is None:
            return TurnResult(status="ignored", transcript=text)
        if key_confirm:
            return TurnResult(status="ignored", turn_id=job.turn.turn_id, transcript=job.turn.transcript)
        command = match_voice_command(text)
        if command is not None and command.kind == "stop":
            return self._kill_job(job, "已停下。")
        if command is not None and command.kind == "background":
            return self._background_job(job)
        if command is not None and command.kind == "resume":
            return TurnResult(status="ignored", turn_id=job.turn.turn_id, transcript=text)
        cleaned = text.strip()
        if not is_usable_transcript(cleaned):
            return TurnResult(status="empty", transcript=cleaned)
        if self._park(job):
            self._emit("上一件已放到后台。")
            self._speak("上一件我放到后台了。", job.turn)
        else:
            self._kill_job(job, "上一件先停下。")
        return self._begin(cleaned, stt_ms=None, images=(), wait=wait)

    def _background_job(self, job: _Job) -> TurnResult:
        if not self._park(job):
            self._emit("已经有一件在后台。")
            self._speak("已经有一件在后台，这一件还在前台。", job.turn)
            return TurnResult(status="running", turn_id=job.turn.turn_id, transcript=job.turn.transcript)
        self._emit("已放到后台。")
        self._speak("好，放到后台。做完了我再告诉你。", job.turn)
        return TurnResult(
            status="background",
            turn_id=job.turn.turn_id,
            transcript=job.turn.transcript,
            session_id=job.turn.session_id,
        )

    def _park(self, job: _Job) -> bool:
        if self._background is not None and not self._background.finalized:
            return False
        job.detached = True
        self._background = job
        if self._foreground is job:
            self._foreground = None
        self.presence.stop()
        with self._lock:
            self.phase = "idle"
        return True

    def _kill_job(self, job: _Job, phrase: str) -> TurnResult:
        job.suppress = True
        job.cancel.cancel()
        if self._foreground is job:
            self._foreground = None
        if self._background is job:
            self._background = None
        self.presence.stop()
        self.playback.stop()
        self._graveyard.append(job)
        with self._lock:
            if self._foreground is None and self._pending is None:
                self.phase = "idle"
        self._emit(phrase)
        self._speak(phrase, job.turn)
        return TurnResult(status="interrupted", turn_id=job.turn.turn_id, transcript=job.turn.transcript)

    def _stop_background(self) -> TurnResult:
        job = self._background
        if job is None:
            self._say_idle("现在没有在做的事。")
            return TurnResult(status="ignored")
        if job.done.is_set() and not job.suppress and job.error is None:
            delivered = self._deliver_background()
            if delivered is not None:
                return delivered
        return self._kill_job(job, "已停下。")

    def _background_running(self) -> _Job | None:
        job = self._background
        if job is None or job.finalized or job.done.is_set() or job.suppress:
            return None
        return job

    def _report_waiting(self) -> bool:
        job = self._background
        return job is not None and job.done.is_set() and not job.finalized and not job.suppress

    def _open_job(self, turn: _Turn, kind: str) -> _Job:
        self.activity.reset()
        self.presence.start()
        return _Job(kind=kind, turn=turn, cancel=CancelToken(), t0=self.timer())

    def _launch(self, job: _Job, fn: Callable[[], object]) -> None:
        with self._lock:
            self._foreground = job
            if not self._speech_cancel.is_set():
                self.phase = "thinking"

        def run() -> None:
            try:
                job.outcome = fn()
            except BaseException as exc:
                job.error = exc
            finally:
                job.done.set()

        job.thread = threading.Thread(target=run, name="orb-codex", daemon=True)
        job.thread.start()

    def _note(self, job: _Job, label: str, tools: int) -> None:
        if self._foreground is not job:
            return
        self.activity.note(label, tools)

    def _wait_job(self, job: _Job) -> TurnResult:
        while True:
            if job.detached:
                return TurnResult(
                    status="background",
                    turn_id=job.turn.turn_id,
                    transcript=job.turn.transcript,
                    session_id=job.turn.session_id,
                )
            if job.done.is_set() and not self._user_speaking:
                result = self._collect_foreground()
                if result is not None:
                    return result
                if job.finalized:
                    return self._result(job.turn, "interrupted")
            self._tick_presence()
            self._reap()
            job.done.wait(0.05)

    def _foreground_running(self) -> _Job | None:
        job = self._foreground
        if job is None or job.finalized or job.detached or job.suppress or job.done.is_set():
            return None
        return job

    def _tick_presence(self) -> None:
        if self._foreground_running() is None:
            return
        phrase = self.presence.tick(quiet=not self._user_speaking and self._pending is None)
        if not phrase:
            return
        job = self._foreground_running()
        if job is None:
            return
        self._emit(phrase)
        self._speak(phrase, job.turn, restore="thinking")

    def _collect_foreground(self) -> TurnResult | None:
        job = self._foreground
        if job is None or job.detached or job.finalized or not job.done.is_set():
            return None
        if self._user_speaking:
            return None
        job.finalized = True
        with self._lock:
            if self._foreground is job:
                self._foreground = None
        self.presence.stop()
        return self._finish_job(job, announce=False)

    def _deliver_background(self) -> TurnResult | None:
        job = self._background
        if job is None or job.finalized or not job.done.is_set():
            return None
        if not self._is_quiet():
            return None
        job.finalized = True
        self._background = None
        # A later barge-in cancels the speech token. The report is a new utterance.
        with self._lock:
            self._speech_cancel = CancelToken()
        if job.suppress or isinstance(job.error, CodexCancelled):
            self._finish(job.turn, "interrupted")
            return self._result(job.turn, "interrupted")
        return self._finish_job(job, announce=True)

    def _is_quiet(self) -> bool:
        if self._user_speaking or self._pending is not None:
            return False
        if self._foreground is not None and not self._foreground.finalized:
            return False
        return self.phase not in {"speaking", "transcribing"}

    def _expire_confirmation(self) -> TurnResult | None:
        with self._lock:
            pending = self._pending
            if pending is None or self.clock() < pending.deadline:
                return None
            self._pending = None
            turn = pending.turn
        self._set_gated(False)
        return self._timeout_turn(turn)

    def _reap(self) -> None:
        still: list[_Job] = []
        for job in self._graveyard:
            if not job.done.is_set():
                still.append(job)
                continue
            if job.finalized:
                continue
            job.finalized = True
            self._finish(job.turn, "interrupted")
        self._graveyard = still

    def _finish_job(self, job: _Job, *, announce: bool) -> TurnResult:
        turn = job.turn
        elapsed = _ms(max(0.0, self.timer() - job.t0))
        if job.suppress or isinstance(job.error, CodexCancelled):
            if job.kind == "execute":
                turn.codex_ms = (turn.codex_ms or 0) + elapsed
            elif turn.codex_ms is None:
                turn.codex_ms = elapsed
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        if isinstance(job.error, CodexTimeout):
            if job.kind == "execute":
                turn.codex_ms = (turn.codex_ms or 0) + elapsed
            else:
                turn.codex_ms = elapsed
            turn.error = str(job.error)
            message = "这一轮时间太长，我先停下了。说继续可以接着聊。"
            self._emit(message)
            self._speak(message, turn)
            self._finish(turn, "error")
            return self._result(turn, "error", error=str(job.error))
        if isinstance(job.error, HighRiskActionBlocked):
            return self._blocked(turn, job.error, elapsed, add=job.kind == "execute")
        if job.error is not None:
            return self._backend_error(turn, job.t0, job.error, add=job.kind == "execute")
        if job.kind == "execute":
            turn.codex_ms = (turn.codex_ms or 0) + elapsed
            return self._after_execute(turn, job.outcome, announce=announce)
        turn.codex_ms = elapsed
        return self._after_plan(turn, job.outcome, announce=announce)

    def _after_plan(self, turn: _Turn, outcome: object, *, announce: bool) -> TurnResult:
        planned = outcome if isinstance(outcome, PlanResult) else _coerce_plan(outcome)
        turn.session_id = planned.session_id
        actions = actions_requiring_confirmation(planned.plan, self.config.codex.sandbox)
        if not actions:
            unsettled = [item for item in planned.side_effects if observation_is_unsettled(item)]
            if unsettled:
                log.warning(
                    "read-only plan saw non-inspection events; speaking the answer and not starting execute: %s",
                    unsettled,
                )
            lead = "刚才那件做完了。" if announce else ""
            return self._speak_answer(turn, planned.text, planned.session_id, lead=lead)
        return self._ask_confirmation(turn, actions, planned.side_effects, announce=announce, reply=planned.text)

    def _ask_confirmation(
        self,
        turn: _Turn,
        actions: tuple[PlannedAction, ...],
        side_effects: tuple[str, ...],
        *,
        announce: bool,
        reply: str,
    ) -> TurnResult:
        turn.actions = actions
        turn.risk = "high"
        key_only = is_key_only(actions, turn.transcript, self.config.codex.sandbox)
        prompt = confirmation_text(actions, side_effects, key_only=key_only)
        if announce:
            prompt = "刚才放到后台的那一轮需要确认。\n" + prompt
        nonce = uuid.uuid4().hex
        with self._lock:
            if self._speech_cancel.is_set():
                cancelled = True
            else:
                cancelled = False
                self._pending = _Pending(
                    nonce=nonce,
                    deadline=self.clock() + self.config.permissions.confirm_timeout_s,
                    turn=turn,
                    key_only=key_only,
                    side_effects=side_effects,
                )
                self.phase = "awaiting_confirmation"
        if cancelled:
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=reply, session_id=turn.session_id)
        self._set_gated(True)
        self._emit(prompt)
        self._speak(prompt, turn)
        with self._lock:
            pending = self._pending
            if pending is not None and pending.nonce == nonce and not self._speech_cancel.is_set():
                self.phase = "awaiting_confirmation"
        return TurnResult(
            status="awaiting_confirmation",
            turn_id=turn.turn_id,
            transcript=turn.transcript,
            risk=turn.risk,
            session_id=turn.session_id,
            reply=reply,
        )

    def _after_execute(self, turn: _Turn, outcome: object, *, announce: bool) -> TurnResult:
        text = str(getattr(outcome, "text", "") or "")
        full = getattr(outcome, "full_text", None)
        session_id = getattr(outcome, "session_id", None) or turn.session_id
        turn.session_id = session_id if isinstance(session_id, str) else turn.session_id
        lead = "刚才那件做完了。" if announce else ""
        return self._speak_answer(turn, text, turn.session_id, full_text=full if isinstance(full, str) else None, lead=lead)

    def _resolve_voice(self, text: str, *, wait: bool) -> TurnResult:
        command = match_voice_command(text)
        if command is not None and command.kind == "stop":
            decision = "no"
        elif command is not None and command.kind == "background":
            with self._lock:
                pending = self._pending
            if pending is None:
                return TurnResult(status="ignored", transcript=text)
            self._emit("还要确认才会执行。")
            self._speak("还要确认才会执行。", pending.turn)
            return TurnResult(
                status="awaiting_confirmation",
                turn_id=pending.turn.turn_id,
                transcript=pending.turn.transcript,
                risk=pending.turn.risk,
            )
        else:
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
            return self._run_execute(turn, wait=wait)
        self._set_gated(False)
        if action == "no":
            message = self.config.permissions.cancel_message
        else:
            message = self.config.permissions.unclear_message
        self._emit(message)
        self._speak(message, turn)
        self._finish(turn, "cancelled")
        return TurnResult(status="cancelled", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)

    def _confirm_with_key(self, *, wait: bool) -> TurnResult:
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
        return self._run_execute(turn, wait=wait)

    def _run_execute(self, turn: _Turn, *, wait: bool) -> TurnResult:
        self._set_gated(False)
        turn.confirmed = True
        turn.permission_ms = max(0, _ms(self.timer() - turn.started) - (turn.stt_ms or 0))
        with self._lock:
            if self._speech_cancel.is_set():
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
        if self._speech_cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted")
        self._emit("已确认，开始执行。")
        job = self._open_job(turn, "execute")

        def fn():
            return self.backend.execute(
                turn.transcript,
                turn.actions,
                images=() if turn.session_id else turn.images,
                resume_id=turn.session_id,
                cancel=job.cancel,
                on_activity=lambda label, tools, bound=job: self._note(bound, label, tools),
            )

        self._launch(job, fn)
        if not wait:
            return TurnResult(status="running", turn_id=turn.turn_id, transcript=turn.transcript, risk=turn.risk)
        return self._wait_job(job)

    def _speak_answer(
        self,
        turn: _Turn,
        text: str,
        session_id: str | None,
        full_text: str | None = None,
        lead: str = "",
    ) -> TurnResult:
        turn.reply_chars = len(text or "")
        turn.session_id = session_id
        if self._speech_cancel.is_set():
            self._finish(turn, "interrupted")
            return self._result(turn, "interrupted", reply=text, session_id=session_id)
        summary = lead + spoken_summary(
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

    def _blocked(self, turn: _Turn, exc: HighRiskActionBlocked, elapsed: int, *, add: bool) -> TurnResult:
        turn.codex_ms = (turn.codex_ms or 0) + elapsed if add else elapsed
        turn.error = str(exc)
        self._emit(self.config.permissions.blocked_message)
        self._speak(self.config.permissions.blocked_message, turn)
        self._finish(turn, "blocked")
        return self._result(turn, "blocked", error=str(exc))

    def _backend_error(self, turn: _Turn, started: float, exc: BaseException, *, add: bool = False) -> TurnResult:
        elapsed = _ms(max(0.0, self.timer() - started))
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

    def _say_idle(self, text: str) -> None:
        self._emit(text)
        self._speak(text, self._blank_turn(""))

    def _speak(self, text: str, turn: _Turn, *, restore: str | None = None) -> bool:
        if not text:
            return True
        if self._speech_cancel.is_set() or self._user_speaking:
            return False
        self.playback.arm()
        started = self.timer()
        path = ""
        try:
            path = self.tts.synthesize(text, turn_id=turn.turn_id)
        except Exception as exc:
            turn.error = f"tts: {exc}"
            self._emit(f"语音合成失败：{exc}")
            log.warning("tts failed: %s", exc)
            return not self._speech_cancel.is_set()
        turn.tts_ms = (turn.tts_ms or 0) + _ms(self.timer() - started)
        if self._speech_cancel.is_set() or self._user_speaking:
            self._delete_tts(path)
            return False
        with self._lock:
            if self._speech_cancel.is_set() or self._user_speaking:
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
            interrupted = self.phase == "interrupted" or self._speech_cancel.is_set()
            if not interrupted and self.phase == "speaking":
                self.phase = restore or "idle"
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
            self._speech_cancel = CancelToken()
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
            if self._foreground is None and self._pending is None:
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
