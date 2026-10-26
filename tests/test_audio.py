import wave
from pathlib import Path

from orb.audio import AudioInput, RMSVAD, floats_to_pcm16, resample_pcm16, rms_pcm16, write_wav
from orb.config import AppConfig


def _pcm(ms: int, amplitude: int, rate: int = 16000) -> bytes:
    count = int(rate * ms / 1000)
    sample = int(amplitude).to_bytes(2, "little", signed=True)
    return sample * count


def test_rms_silence_and_tone():
    assert rms_pcm16(b"") == 0
    assert rms_pcm16(_pcm(20, 0)) == 0
    assert rms_pcm16(_pcm(20, 8000)) > 0.2


def test_floats_to_pcm16_without_assuming_numpy():
    pcm = floats_to_pcm16([0.0, 1.0, -1.0])
    assert len(pcm) == 6
    assert int.from_bytes(pcm[2:4], "little", signed=True) == 32767


def test_vad_writes_speech_wav(tmp_path: Path):
    config = AppConfig()
    config = config.__class__(
        **{
            **config.__dict__,
            "paths": config.paths.__class__(audio_dir=str(tmp_path), delete_audio_after_stt=True),
            "audio": config.audio.__class__(
                mode="vad",
                sample_rate=16000,
                vad=config.audio.vad.__class__(threshold=0.02, start_ms=100, end_ms=200),
            ),
        }
    )
    events = []
    audio = AudioInput(config, events.append)
    audio.on_pcm(_pcm(150, 8000))
    audio.on_pcm(_pcm(250, 0))
    kinds = [event.kind for event in events]
    assert kinds == ["speech_start", "speech_end"]
    wav_path = events[1].wav_path
    assert wav_path is not None
    with wave.open(wav_path) as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getnframes() > 0


def test_push_to_talk_and_short_press(tmp_path: Path):
    config = AppConfig()
    audio_config = config.audio.__class__(
        mode="ptt",
        sample_rate=16000,
        min_utterance_ms=250,
        vad=config.audio.vad,
    )
    paths = config.paths.__class__(audio_dir=str(tmp_path), delete_audio_after_stt=True)
    config = config.__class__(**{**config.__dict__, "audio": audio_config, "paths": paths})
    events = []
    audio = AudioInput(config, events.append)
    audio.set_ptt(True)
    audio.on_pcm(_pcm(100, 8000))
    audio.set_ptt(False)
    assert [event.kind for event in events] == ["speech_start"]

    audio.set_ptt(True)
    audio.on_pcm(_pcm(300, 8000))
    audio.set_ptt(False)
    assert events[-1].kind == "speech_end"
    assert events[-1].wav_path
    assert Path(events[-1].wav_path).stat().st_size > 44


def test_gate_drops_vad_audio(tmp_path: Path):
    config = AppConfig()
    audio_config = config.audio.__class__(
        mode="vad",
        sample_rate=16000,
        vad=config.audio.vad.__class__(threshold=0.02, start_ms=50, end_ms=50),
    )
    paths = config.paths.__class__(audio_dir=str(tmp_path), delete_audio_after_stt=True)
    config = config.__class__(**{**config.__dict__, "audio": audio_config, "paths": paths})
    events = []
    audio = AudioInput(config, events.append)
    audio.set_gated(True)
    audio.on_pcm(_pcm(200, 8000))
    audio.on_pcm(_pcm(200, 0))
    assert events == []


def test_write_wav_round_trip(tmp_path: Path):
    path = write_wav(tmp_path / "nested" / "a.wav", _pcm(20, 1000), 16000)
    with wave.open(path) as handle:
        assert handle.readframes(handle.getnframes()) == _pcm(20, 1000)
    assert (tmp_path / "nested").stat().st_mode & 0o777 == 0o700
    assert Path(path).stat().st_mode & 0o777 == 0o600


def test_sounddevice_uses_device_rate_and_default_blocksize(monkeypatch):
    import sys
    import types

    fake = types.ModuleType("sounddevice")
    fake.last = {}
    fake.checked = {}

    def query_devices(device, kind):
        assert kind == "input"
        return {"default_samplerate": 48000.0}

    def check_input_settings(**kwargs):
        fake.checked = kwargs

    class InputStream:
        def __init__(self, **kwargs):
            fake.last = kwargs

        def start(self):
            return None

    fake.query_devices = query_devices
    fake.check_input_settings = check_input_settings
    fake.InputStream = InputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    from orb.audio import open_sounddevice_stream

    open_sounddevice_stream(AppConfig(), lambda _pcm: None)
    assert fake.checked["samplerate"] == 48000
    assert fake.last["samplerate"] == 48000
    assert fake.last["blocksize"] == 0


def test_resample_changes_length():
    pcm = _pcm(1000, 1000, rate=16000)
    out = resample_pcm16(pcm, 16000, 8000)
    assert abs(len(out) // 2 - 8000) <= 1


def test_push_to_talk_stops_at_max_utterance(tmp_path: Path):
    config = AppConfig()
    audio_config = config.audio.__class__(
        mode="ptt",
        sample_rate=16000,
        min_utterance_ms=50,
        max_utterance_s=0.2,
        vad=config.audio.vad,
    )
    paths = config.paths.__class__(audio_dir=str(tmp_path), delete_audio_after_stt=True)
    config = config.__class__(**{**config.__dict__, "audio": audio_config, "paths": paths})
    events = []
    audio = AudioInput(config, events.append)
    audio.set_ptt(True)
    audio.on_pcm(_pcm(300, 8000))
    assert events[-1].kind == "speech_end"
    audio.on_pcm(_pcm(300, 8000))
    assert [event.kind for event in events].count("speech_end") == 1


def test_vad_class_end_event():
    vad = RMSVAD(16000, 0.02, 100, 200)
    assert vad.feed(_pcm(150, 8000)) == ["start"]
    assert vad.feed(_pcm(250, 0)) == ["end"]
    assert len(vad.take_audio()) > 0
