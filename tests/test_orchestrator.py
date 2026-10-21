import json
import threading
from dataclasses import replace

from orb.backends.base import BackendResult, HighRiskActionBlocked
from orb.config import AppConfig
from orb.metrics import MetricsLog
from orb.orchestrator import Orchestrator


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
        self.always = False

    def always_requires_confirmation(self):
        return self.always

    def ask(self, prompt, *, confirmed=False, images=(), cancel=None):
        self.calls.append({"prompt": prompt, "confirmed": confirmed, "images": tuple(images)})
        if self.clock is not None:
            self.clock.advance(1.5)
        if cancel is not None and cancel.is_set():
            from orb.backends.base import CodexCancelled

            raise CodexCancelled("cancelled")
        if self.error:
            raise self.error
        return BackendResult(text=self.text, full_text=self.text, session_id="thread-1")


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


def _orch(tmp_path, backend, tts=None, playback=None, stt=None, clock=None, ack="好。"):
    config = AppConfig()
    config = replace(
        config,
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


def test_high_risk_waits_for_exact_confirmation(tmp_path):
    backend = FakeBackend()
    tts = FakeTTS()
    _config, orchestrator = _orch(tmp_path, backend, tts=tts)
    pending = orchestrator.submit_text("把临时文件删掉")
    assert pending.status == "awaiting_confirmation"
    assert backend.calls == []
    assert tts.texts == [orchestrator.config.permissions.confirm_prompt]
    cancelled = orchestrator.submit_text("先别")
    assert cancelled.status == "cancelled"
    assert backend.calls == []

    pending = orchestrator.submit_text("把临时文件删掉")
    confirmed = orchestrator.submit_text("确认")
    assert confirmed.status == "ok"
    assert backend.calls[-1]["confirmed"] is True
    assert backend.calls[-1]["prompt"] == "把临时文件删掉"


def test_no_and_timeout_do_not_call_codex(tmp_path):
    clock = Clock()
    backend = FakeBackend(clock)
    _config, orchestrator = _orch(tmp_path, backend, clock=clock)
    orchestrator.submit_text("请删除缓存")
    assert orchestrator.submit_text("取消").status == "cancelled"
    orchestrator.submit_text("请删除缓存")
    clock.advance(11)
    assert orchestrator.poll().status == "timeout"
    assert backend.calls == []


def test_confirm_key(tmp_path):
    backend = FakeBackend()
    _config, orchestrator = _orch(tmp_path, backend)
    orchestrator.submit_text("git push origin main")
    result = orchestrator.confirm_with_key()
    assert result.status == "ok"
    assert backend.calls[0]["confirmed"] is True


def test_unconfirmed_tool_block_is_spoken(tmp_path):
    backend = FakeBackend()
    backend.error = HighRiskActionBlocked("rm -rf /tmp/x")
    tts = FakeTTS()
    _config, orchestrator = _orch(tmp_path, backend, tts=tts)
    result = orchestrator.submit_text("列一下文件")
    assert result.status == "blocked"
    assert tts.texts[-1] == orchestrator.config.permissions.blocked_message
    assert "rm -rf" not in tts.texts[-1]


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
