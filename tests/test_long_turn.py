"""Filler, stuck, background, and resume. The confirmation gate still holds."""

import threading
import time
from dataclasses import replace

from orb.backends.base import BackendResult, CodexCancelled
from orb.config import AppConfig
from orb.metrics import MetricsLog
from orb.orchestrator import Orchestrator
from orb.plan import ExecutionPlan, PlannedAction, PlanResult
from orb.voice_commands import RESUME_PROMPT


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class BlockingBackend:
    def __init__(self, text="这是口语结论。已经处理完。"):
        self.text = text
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []
        self.cancel = None
        self.hook = None
        self.forced_plan = None
        self.thread_id = "thread-1"

    def saved_thread_id(self):
        return self.thread_id

    def plan(self, prompt, *, images=(), cancel=None, on_activity=None):
        self.calls.append({"phase": "plan", "prompt": prompt})
        self.cancel = cancel
        self.hook = on_activity
        self.started.set()
        while not self.release.is_set():
            if cancel is not None and cancel.is_set():
                raise CodexCancelled("cancelled")
            self.release.wait(0.01)
        if self.forced_plan is not None:
            return self.forced_plan
        if any(word in prompt for word in ("删", "delete")):
            plan = ExecutionPlan(
                spoken="将删除临时文件。",
                execute=True,
                actions=(PlannedAction("delete", prompt, "high"),),
            )
            return PlanResult(plan=plan, session_id=self.thread_id or "thread-1")
        plan = ExecutionPlan(spoken=self.text, execute=False, actions=())
        return PlanResult(plan=plan, session_id=self.thread_id or "thread-1")

    def execute(self, prompt, actions, *, images=(), resume_id=None, cancel=None, on_activity=None):
        self.calls.append({"phase": "execute", "prompt": prompt, "actions": tuple(actions)})
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled")
        return BackendResult(text=self.text, full_text=self.text, session_id=resume_id or "thread-1")


class TTS:
    def __init__(self):
        self.texts = []

    def synthesize(self, text, *, turn_id):
        self.texts.append(text)
        return ""


class Playback:
    def arm(self):
        return None

    def play(self, path):
        return None

    def stop(self):
        return None


class HoldingPlayback:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.stopped = 0

    def arm(self):
        return None

    def play(self, path):
        self.started.set()
        self.release.wait(2)

    def stop(self):
        self.stopped += 1
        self.release.set()


def _orch(tmp_path, backend, *, presence, clock=None, ack="好。", playback=None, tts=None):
    config = replace(
        AppConfig(),
        presence=presence,
        tts=replace(AppConfig().tts, ack=ack, output_dir=str(tmp_path / "tts")),
        metrics=replace(AppConfig().metrics, path=str(tmp_path / "metrics.jsonl")),
        paths=replace(AppConfig().paths, audio_dir=str(tmp_path / "audio")),
        session=replace(AppConfig().session, path=str(tmp_path / "session.json")),
    )
    orchestrator = Orchestrator(
        config,
        backend,
        None,
        tts or TTS(),
        playback or Playback(),
        MetricsLog(config.metrics.path),
        timer=clock,
        clock=clock,
    )
    return orchestrator


def _wait_status(orchestrator, status, timeout=2.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = orchestrator.poll()
        if last is not None and last.status == status:
            return last
        time.sleep(0.02)
    return last


def test_fast_plan_does_not_speak_filler(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    backend.release.set()
    presence = replace(AppConfig().presence, filler_delay_s=30, progress_start_s=0, stuck_after_s=0)
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, tts=tts)
    result = orchestrator.submit_text("现在几点")
    assert result.status == "ok"
    assert "我看一下。" not in tts.texts


def test_filler_is_spoken_and_barge_in_does_not_kill_the_plan(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    presence = replace(
        AppConfig().presence,
        filler_delay_s=0,
        filler="我看一下。",
        progress_start_s=0,
        stuck_after_s=0,
    )
    playback = HoldingPlayback()
    tts = TTS()
    # synthesize returns "" in TTS, so give the filler a path by wrapping.
    class PathTTS(TTS):
        def synthesize(self, text, *, turn_id):
            self.texts.append(text)
            return f"/tmp/{turn_id}.aiff"

    tts = PathTTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", playback=playback, tts=tts)
    box = {}

    def run():
        box["result"] = orchestrator.submit_text("现在几点", wait=True)

    thread = threading.Thread(target=run)
    thread.start()
    assert backend.started.wait(2)
    assert playback.started.wait(2)
    orchestrator.on_speech_start()
    assert playback.stopped >= 1
    assert backend.cancel is not None and not backend.cancel.is_set()
    orchestrator._user_speaking = False
    backend.release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert box["result"].status == "ok"
    assert tts.texts[0] == "我看一下。"


def test_filler_does_not_talk_over_the_user(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    presence = replace(AppConfig().presence, filler_delay_s=0, progress_start_s=0, stuck_after_s=0)
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", tts=tts)
    try:
        orchestrator.submit_text("现在几点", wait=False)
        assert backend.started.wait(2)
        orchestrator.on_speech_start()
        orchestrator.poll()
        assert "我看一下。" not in tts.texts
        stopped = orchestrator.submit_text("停", wait=False)
        assert stopped.status == "interrupted"
        assert backend.cancel.is_set()
    finally:
        backend.release.set()


def test_progress_mentions_the_running_command_and_stuck_waits(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    presence = replace(
        AppConfig().presence,
        filler="",
        progress_start_s=5,
        progress_max_s=10,
        stuck_after_s=5,
        stuck="一会儿没有新消息，可能卡住了。",
    )
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", tts=tts)
    try:
        orchestrator.submit_text("看一下仓库", wait=False)
        assert backend.started.wait(2)
        backend.hook("在看文件", 1)
        clock.advance(5)
        orchestrator.poll()
        assert any("在看文件" in text for text in tts.texts)
        clock.advance(30)
        orchestrator.poll()
        assert not any("卡住" in text for text in tts.texts)
        backend.hook("在看文件", 0)
        clock.advance(5)
        orchestrator.poll()
        assert any("卡住" in text for text in tts.texts)
    finally:
        backend.release.set()


def test_background_read_only_reports_later_and_stop_kills(tmp_path):
    clock = Clock()
    backend = BlockingBackend(text="这个目录是语音助手。")
    presence = replace(AppConfig().presence, filler="", progress_start_s=0, stuck_after_s=0)
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", tts=tts)
    orchestrator.submit_text("用两句话说明当前目录是做什么的", wait=False)
    assert backend.started.wait(2)
    parked = orchestrator.submit_text("后台做")
    assert parked.status == "background"
    assert not backend.cancel.is_set()
    assert [call["phase"] for call in backend.calls] == ["plan"]
    orchestrator.on_speech_start()
    orchestrator._user_speaking = False
    backend.release.set()
    done = _wait_status(orchestrator, "ok")
    assert done is not None and done.status == "ok"
    assert any("做完了" in text for text in tts.texts)
    assert [call["phase"] for call in backend.calls] == ["plan"]

    backend.release.clear()
    backend.started.clear()
    orchestrator.submit_text("再看一眼", wait=False)
    assert backend.started.wait(2)
    stopped = orchestrator.submit_text("取消")
    assert stopped.status == "interrupted"
    assert backend.cancel.is_set()
    assert "已停下。" in tts.texts
    backend.release.set()
    time.sleep(0.05)
    orchestrator.poll()
    assert [call["phase"] for call in backend.calls] == ["plan", "plan"]


def test_background_write_still_confirms_and_delete_needs_the_key(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    backend.forced_plan = PlanResult(
        plan=ExecutionPlan(
            spoken="将写入备注。",
            execute=True,
            actions=(PlannedAction("write", "写入备注", "low"),),
        )
    )
    presence = replace(AppConfig().presence, filler="", progress_start_s=0, stuck_after_s=0)
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", tts=tts)
    orchestrator.submit_text("写入备注", wait=False)
    assert backend.started.wait(2)
    assert orchestrator.submit_text("先这样").status == "background"
    backend.release.set()
    pending = _wait_status(orchestrator, "awaiting_confirmation")
    assert pending is not None
    assert [call["phase"] for call in backend.calls] == ["plan"]
    assert orchestrator.submit_text("确认").status == "ok"
    assert backend.calls[-1]["phase"] == "execute"

    backend.release.clear()
    backend.started.clear()
    backend.forced_plan = PlanResult(
        plan=ExecutionPlan(
            spoken="将删除临时文件。",
            execute=True,
            actions=(PlannedAction("delete", "删除临时文件", "high"),),
        )
    )
    orchestrator.submit_text("把临时文件删掉", wait=False)
    assert backend.started.wait(2)
    orchestrator.submit_text("放到后台")
    backend.release.set()
    assert _wait_status(orchestrator, "awaiting_confirmation") is not None
    assert orchestrator.submit_text("确认").status == "awaiting_confirmation"
    assert orchestrator.confirm_with_key().status == "ok"
    assert backend.calls[-1]["phase"] == "execute"
    assert any("确认键" in text for text in tts.texts)


def test_resume_phrases_use_the_saved_thread_and_keep_the_gate(tmp_path):
    clock = Clock()
    backend = BlockingBackend()
    backend.release.set()
    presence = replace(AppConfig().presence, filler_delay_s=30, progress_start_s=0, stuck_after_s=0)
    tts = TTS()
    orchestrator = _orch(tmp_path, backend, presence=presence, clock=clock, ack="", tts=tts)
    backend.thread_id = None
    missing = orchestrator.submit_text("接着上次聊")
    assert missing.status == "ignored"
    assert backend.calls == []
    assert any("没有上次" in text for text in tts.texts)

    backend.thread_id = "thread-9"
    followed = orchestrator.submit_text("继续，那个数字是多少")
    assert followed.status == "ok"
    assert backend.calls[-1]["prompt"] == "那个数字是多少"

    alone = orchestrator.submit_text("继续")
    assert alone.status == "ok"
    assert backend.calls[-1]["prompt"] == RESUME_PROMPT

    risky = orchestrator.submit_text("继续把临时文件删掉")
    assert risky.status == "awaiting_confirmation"
    assert orchestrator.submit_text("确认").status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls].count("execute") == 0
    assert orchestrator.confirm_with_key().status == "ok"
    assert backend.calls[-1]["phase"] == "execute"
