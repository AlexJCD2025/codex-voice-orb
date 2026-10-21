import json
import threading
from dataclasses import replace

from orb.backends.base import BackendResult, CodexCancelled
from orb.config import AppConfig
from orb.metrics import MetricsLog
from orb.orchestrator import Orchestrator
from orb.permissions import classify_risk
from orb.plan import ExecutionPlan, PlannedAction, PlanResult


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class FakeBackend:
    def __init__(self, clock=None, text="这是口语结论。后面还有一句。"):
        self.clock = clock
        self.calls = []
        self.text = text
        self.error = None
        self.side_effects: tuple[str, ...] = ()
        self.forced_plan = None
        self.always = False

    def always_requires_confirmation(self):
        return self.always

    def plan(self, prompt, *, images=(), cancel=None):
        self.calls.append({"phase": "plan", "prompt": prompt, "images": tuple(images), "confirmed": False})
        if self.clock is not None:
            self.clock.advance(1.5)
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled")
        if self.error:
            raise self.error
        if self.forced_plan is not None:
            return self.forced_plan
        if classify_risk(prompt) == "high":
            kind = "command"
            if any(word in prompt for word in ("删", "delete", "rm ")):
                kind = "delete"
            elif "deploy" in prompt.lower() or "部署" in prompt:
                kind = "deploy"
            elif "发邮件" in prompt or "send" in prompt.lower():
                kind = "send"
            elif "付款" in prompt or "pay" in prompt.lower():
                kind = "pay"
            plan = ExecutionPlan(
                spoken="将要处理：" + prompt,
                execute=True,
                actions=(PlannedAction(kind, prompt, "high"),),
            )
        else:
            plan = ExecutionPlan(spoken=self.text, execute=False, actions=())
        return PlanResult(plan=plan, session_id="thread-1", side_effects=self.side_effects)

    def execute(self, prompt, actions, *, images=(), resume_id=None, cancel=None):
        self.calls.append(
            {
                "phase": "execute",
                "prompt": prompt,
                "actions": tuple(actions),
                "images": tuple(images),
                "resume_id": resume_id,
                "confirmed": True,
            }
        )
        if self.clock is not None:
            self.clock.advance(1.5)
        if cancel is not None and cancel.is_set():
            raise CodexCancelled("cancelled")
        return BackendResult(text=self.text, full_text=self.text, session_id=resume_id or "thread-1")


class FakeTTS:
    def __init__(self, path=False):
        self.texts = []
        self.path = path

    def synthesize(self, text, *, turn_id):
        self.texts.append(text)
        if self.path:
            return f"/tmp/{turn_id}.aiff"
        return ""


class FakePlayback:
    def __init__(self):
        self.played = []
        self.stopped = 0

    def arm(self):
        return None

    def play(self, path):
        self.played.append(path)

    def stop(self):
        self.stopped += 1


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


class FakeSTT:
    def __init__(self, text="你好"):
        self.text = text
        self.paths = []

    def transcribe(self, path):
        self.paths.append(path)
        return self.text


def _orch(tmp_path, backend, tts=None, playback=None, stt=None, clock=None, ack="好。", sandbox="read-only"):
    config = AppConfig()
    config = replace(
        config,
        codex=replace(config.codex, sandbox=sandbox),
        tts=replace(config.tts, ack=ack, output_dir=str(tmp_path / "tts")),
        metrics=replace(config.metrics, path=str(tmp_path / "metrics.jsonl")),
        paths=replace(config.paths, audio_dir=str(tmp_path / "audio"), delete_audio_after_stt=True),
        session=replace(config.session, path=str(tmp_path / "session.json")),
    )
    orchestrator = Orchestrator(
        config,
        backend,
        stt or FakeSTT(),
        tts or FakeTTS(),
        playback or FakePlayback(),
        MetricsLog(config.metrics.path),
        timer=clock,
        clock=clock,
    )
    return config, orchestrator


def _records(tmp_path):
    path = tmp_path / "metrics.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_low_risk_turn_speaks_summary_and_logs_latency(tmp_path):
    clock = Clock()
    backend = FakeBackend(clock)
    tts = FakeTTS()
    _config, orchestrator = _orch(tmp_path, backend, tts=tts, clock=clock)
    result = orchestrator.submit_text("现在几点")
    assert result.status == "ok"
    assert result.session_id == "thread-1"
    assert result.summary.startswith("这是口语结论。")
    assert backend.calls[0]["phase"] == "plan"
    assert backend.calls[0]["prompt"] == "现在几点"
    assert backend.calls[0]["confirmed"] is False
    assert tts.texts[0] == "好。"
    assert tts.texts[-1] == result.summary
    record = _records(tmp_path)[0]
    assert record["status"] == "ok"
    assert record["risk"] == "low"
    assert record["codex_ms"] >= 1500
    assert record["total_ms"] >= 1500
    assert "现在几点" not in json.dumps(record)
    assert record["transcript_chars"] == len("现在几点")


def test_high_risk_plans_before_confirmation_and_delete_is_key_only(tmp_path):
    backend = FakeBackend()
    tts = FakeTTS()
    gated = []
    _config, orchestrator = _orch(tmp_path, backend, tts=tts)
    orchestrator.set_gated = gated.append
    pending = orchestrator.submit_text("把临时文件删掉")
    assert pending.status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls] == ["plan"]
    assert gated[-1] is True
    assert "删除" in tts.texts[-1] or "删掉" in tts.texts[-1]
    assert "确认键" in tts.texts[-1]
    assert orchestrator.submit_text("确认").status == "awaiting_confirmation"
    assert orchestrator.submit_text("好").status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls] == ["plan"]
    cancelled = orchestrator.submit_text("取消")
    assert cancelled.status == "cancelled"
    assert gated[-1] is False

    orchestrator.submit_text("把临时文件删掉")
    confirmed = orchestrator.confirm_with_key()
    assert confirmed.status == "ok"
    assert backend.calls[-1]["phase"] == "execute"
    assert backend.calls[-1]["confirmed"] is True
    assert backend.calls[-1]["prompt"] == "把临时文件删掉"
    assert gated[-1] is False


def test_no_and_timeout_do_not_execute(tmp_path):
    clock = Clock()
    backend = FakeBackend(clock)
    _config, orchestrator = _orch(tmp_path, backend, clock=clock)
    orchestrator.submit_text("请删除缓存")
    assert orchestrator.submit_text("取消").status == "cancelled"
    orchestrator.submit_text("请删除缓存")
    clock.advance(11)
    assert orchestrator.poll().status == "timeout"
    assert [call["phase"] for call in backend.calls] == ["plan", "plan"]


def test_confirm_key_and_voice_yes_for_a_command(tmp_path):
    backend = FakeBackend()
    _config, orchestrator = _orch(tmp_path, backend)
    orchestrator.submit_text("git push origin main")
    result = orchestrator.confirm_with_key()
    assert result.status == "ok"
    assert backend.calls[-1]["phase"] == "execute"
    assert backend.calls[-1]["confirmed"] is True

    orchestrator.submit_text("git push origin main")
    voiced = orchestrator.submit_text("yes")
    assert voiced.status == "ok"
    assert backend.calls[-1]["phase"] == "execute"


READ_PWD = "/bin/zsh -lc \"pwd && rg --files -g 'README*' | head -30\""
READ_SED = "/bin/zsh -lc \"sed -n '1,180p' README.md && sed -n '1,160p' pyproject.toml\""


def test_readonly_plan_commands_finish_without_confirmation(tmp_path):
    from orb.plan import command_is_mutating, command_is_readonly

    assert command_is_readonly(READ_PWD)
    assert command_is_readonly(READ_SED)
    assert not command_is_mutating(READ_PWD)
    assert not command_is_readonly("/bin/zsh -lc 'rm -rf /tmp/secret'")
    assert command_is_mutating("/bin/zsh -lc 'rm -rf /tmp/secret'")
    backend = FakeBackend(text="这个目录是语音助手。它把麦接到 Codex。")
    backend.side_effects = (READ_PWD, READ_SED)
    tts = FakeTTS()
    _config, orchestrator = _orch(tmp_path, backend, tts=tts)
    result = orchestrator.submit_text("用两句话说明当前目录是做什么的")
    assert result.status == "ok"
    assert "语音助手" in result.summary
    assert [call["phase"] for call in backend.calls] == ["plan"]
    assert "确认键" not in "\n".join(tts.texts)


def test_already_ran_rm_does_not_hide_the_answer(tmp_path):
    backend = FakeBackend()
    backend.side_effects = ("/bin/zsh -lc 'rm -rf /tmp/x'",)
    _config, orchestrator = _orch(tmp_path, backend)
    result = orchestrator.submit_text("列一下文件")
    assert result.status == "ok"
    assert [call["phase"] for call in backend.calls] == ["plan"]


def test_write_plan_still_confirms_and_delete_stays_key_only(tmp_path):
    backend = FakeBackend()
    backend.forced_plan = PlanResult(
        plan=ExecutionPlan(
            spoken="将写入备注。",
            execute=True,
            actions=(PlannedAction("write", "写入备注", "low"),),
        ),
        side_effects=(READ_PWD,),
    )
    _config, orchestrator = _orch(tmp_path, backend)
    pending = orchestrator.submit_text("写入备注")
    assert pending.status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls] == ["plan"]
    confirmed = orchestrator.submit_text("确认")
    assert confirmed.status == "ok"
    assert backend.calls[-1]["phase"] == "execute"

    backend.calls.clear()
    backend.forced_plan = PlanResult(
        plan=ExecutionPlan(
            spoken="将删除临时文件。",
            execute=True,
            actions=(PlannedAction("delete", "删除临时文件", "high"),),
        ),
        side_effects=("/bin/zsh -lc 'rm -rf /tmp/secret'",),
    )
    assert orchestrator.submit_text("把临时文件删掉").status == "awaiting_confirmation"
    assert orchestrator.submit_text("确认").status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls] == ["plan"]
    assert orchestrator.confirm_with_key().status == "ok"
    assert backend.calls[-1]["phase"] == "execute"


def test_wider_sandbox_confirms_before_a_second_process(tmp_path):
    backend = FakeBackend()
    backend.forced_plan = PlanResult(
        plan=ExecutionPlan(
            spoken="看完了。",
            execute=True,
            actions=(PlannedAction("command", READ_PWD, "low"),),
        ),
    )
    _config, orchestrator = _orch(tmp_path, backend, sandbox="workspace-write")
    assert orchestrator.submit_text("现在几点").status == "awaiting_confirmation"
    assert [call["phase"] for call in backend.calls] == ["plan"]


def test_barge_in_during_ack_skips_codex(tmp_path):
    backend = FakeBackend()
    playback = HoldingPlayback()
    _config, orchestrator = _orch(tmp_path, backend, tts=FakeTTS(path=True), playback=playback)
    box = {}

    def run():
        box["result"] = orchestrator.submit_text("现在几点")

    thread = threading.Thread(target=run)
    thread.start()
    assert playback.started.wait(2)
    orchestrator.on_speech_start()
    thread.join(2)
    assert not thread.is_alive()
    assert box["result"].status == "interrupted"
    assert backend.calls == []
    assert playback.stopped >= 1


def test_barge_in_during_summary_keeps_the_reply(tmp_path):
    backend = FakeBackend()
    playback = HoldingPlayback()
    _config, orchestrator = _orb_without_ack(tmp_path, backend, playback)
    box = {}

    def run():
        box["result"] = orchestrator.submit_text("现在几点")

    thread = threading.Thread(target=run)
    thread.start()
    assert playback.started.wait(2)
    orchestrator.on_speech_start()
    thread.join(2)
    assert box["result"].status == "interrupted"
    assert box["result"].reply.startswith("这是口语结论")
    assert len(backend.calls) == 1
    record = _records(tmp_path)[-1]
    assert record["interrupted"] is True


def _orb_without_ack(tmp_path, backend, playback):
    return _orch(tmp_path, backend, tts=FakeTTS(path=True), playback=playback, ack="")


def test_handle_wav_deletes_audio(tmp_path):
    backend = FakeBackend()
    stt = FakeSTT("hello orb")
    _config, orchestrator = _orch(tmp_path, backend, stt=stt, ack="")
    wav = tmp_path / "audio" / "in.wav"
    wav.parent.mkdir(parents=True)
    wav.write_bytes(b"RIFFxxxx")
    result = orchestrator.handle_wav(str(wav))
    assert result.status == "ok"
    assert result.transcript == "hello orb"
    assert not wav.exists()
    assert stt.paths == [str(wav)]


def test_silence_is_dropped(tmp_path):
    backend = FakeBackend()
    _config, orchestrator = _orch(tmp_path, backend, stt=FakeSTT("[silence]"))
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"not a real wav")
    result = orchestrator.handle_wav(str(wav))
    assert result.status == "empty"
    assert backend.calls == []


def test_images_reach_the_backend(tmp_path):
    backend = FakeBackend()
    _config, orchestrator = _orch(tmp_path, backend, ack="")
    result = orchestrator.submit_text("描述这张图", images=["/tmp/a.png"])
    assert result.status == "ok"
    assert backend.calls[0]["images"] == ("/tmp/a.png",)
