import subprocess
from dataclasses import replace

import pytest

from orb.config import AppConfig
from orb.stt import STTError, WhisperSTT, is_usable_transcript, whisper_cpp_argv
from orb.tts import NoTTS, SayTTS, TTSError


def test_usable_transcript():
    assert is_usable_transcript("你好 hello")
    assert not is_usable_transcript("  ")
    assert not is_usable_transcript("[silence]")
    assert not is_usable_transcript("...")


def test_fake_faster_whisper_model(tmp_path):
    class Segment:
        def __init__(self, text):
            self.text = text

    class Model:
        def transcribe(self, wav_path, language=None, vad_filter=True):
            assert language is None
            assert vad_filter is True
            return [Segment("你好"), Segment(" hello")], None

    stt = WhisperSTT(AppConfig(), model=Model())
    assert stt.transcribe(str(tmp_path / "a.wav")) == "你好 hello"


def test_missing_faster_whisper_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("no module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(STTError, match="faster-whisper"):
        WhisperSTT(AppConfig()).transcribe("missing.wav")


def test_whisper_cpp_argv(tmp_path):
    model = tmp_path / "ggml-small.bin"
    model.write_bytes(b"model")
    argv = whisper_cpp_argv("whisper-cli", str(model), "in.wav", "auto", "out")
    assert argv[:4] == ["whisper-cli", "-m", str(model), "-f"]
    assert "-l" in argv and "auto" in argv
    config = replace(
        AppConfig(),
        stt=replace(AppConfig().stt, engine="whisper.cpp", model_path=str(model), language="zh"),
    )

    def fake_run(argv, **kwargs):
        assert argv[argv.index("-l") + 1] == "zh"
        open(argv[argv.index("-of") + 1] + ".txt", "w", encoding="utf-8").write("中文结果\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("orb.stt.subprocess.run", fake_run)
    wav = tmp_path / "in.wav"
    wav.write_bytes(b"")
    try:
        assert WhisperSTT(config).transcribe(str(wav)) == "中文结果"
        assert not (tmp_path / "in.txt").exists()
    finally:
        monkeypatch.undo()


def test_say_argv(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("orb.tts.subprocess.run", fake_run)
    config = replace(AppConfig(), tts=replace(AppConfig().tts, voice="Ting-Ting", rate=180, output_dir=str(tmp_path)))
    path = SayTTS(config).synthesize("你好", turn_id="t1")
    assert path.endswith("t1-1.aiff")
    argv = seen["argv"]
    assert argv[:2] == ["say", "-o"]
    assert "-v" in argv and "Ting-Ting" in argv
    assert "-r" in argv and "180" in argv
    assert argv[-1] == "你好"


def test_say_missing(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("say")

    monkeypatch.setattr("orb.tts.subprocess.run", fake_run)
    with pytest.raises(TTSError, match="say"):
        SayTTS(AppConfig()).synthesize("hi", turn_id="t")


def test_no_tts():
    assert NoTTS().synthesize("hi", turn_id="t") == ""
