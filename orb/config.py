"""YAML configuration for codex-voice-orb."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from orb.codex_safety import unsafe_config_reason

class ConfigError(ValueError):
    """The config file is missing, malformed, or asks for an unsafe default."""


def _expand(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return str(Path(value).expanduser())


@dataclass(frozen=True)
class VadConfig:
    threshold: float = 0.02
    start_ms: int = 200
    end_ms: int = 700


@dataclass(frozen=True)
class AudioConfig:
    mode: str = "ptt"
    input_device: str | int | None = None
    sample_rate: int = 16000
    channels: int = 1
    hotkey: str = "ctrl+space"
    confirm_key: str = "ctrl+shift+y"
    min_utterance_ms: int = 250
    gate_during_playback: bool = False
    vad: VadConfig = field(default_factory=VadConfig)


@dataclass(frozen=True)
class SttConfig:
    engine: str = "faster-whisper"
    model: str = "small"
    model_path: str | None = None
    language: str = "auto"
    device: str = "cpu"
    compute_type: str = "int8"
    whisper_cpp_bin: str = "whisper-cli"


@dataclass(frozen=True)
class TtsConfig:
    engine: str = "say"
    voice: str | None = None
    rate: int | None = None
    max_chars: int = 280
    max_sentences: int = 2
    ack: str = "好。"
    output_dir: str = "~/.codex-voice-orb/tts"


@dataclass(frozen=True)
class PlaybackConfig:
    player: str = "auto"


@dataclass(frozen=True)
class CodexConfig:
    bin: str = "codex"
    sandbox: str = "read-only"
    model: str | None = None
    config_overrides: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    cwd: str | None = None
    extra_args: tuple[str, ...] = ()
    timeout_s: float = 180.0
    resume: str = "id"
    voice_preamble: str = (
        "你正在通过语音助手回答。请先用一两句口语给出可以直接朗读的结论，"
        "不要使用 Markdown、代码块或链接。需要时再补充短细节。"
    )
    skip_git_repo_check: bool = True


@dataclass(frozen=True)
class PermissionsConfig:
    confirm_timeout_s: float = 10.0
    allow_full_auto: bool = False
    allow_danger_full_access: bool = False
    confirm_prompt: str = "这是高风险操作。请说“确认”，或按确认键。十秒内未确认会取消。"
    cancel_message: str = "已取消，没有执行。"
    timeout_message: str = "超时未确认，已取消。"
    unclear_message: str = "没有听清确认，已取消。"
    blocked_message: str = "已拦住高风险操作，没有继续执行。"


@dataclass(frozen=True)
class SessionConfig:
    path: str = "~/.codex-voice-orb/session.json"
    bind_cwd: bool = True


@dataclass(frozen=True)
class MetricsConfig:
    path: str = "~/.codex-voice-orb/metrics.jsonl"


@dataclass(frozen=True)
class PathsConfig:
    audio_dir: str = "~/.codex-voice-orb/audio"
    delete_audio_after_stt: bool = True


@dataclass(frozen=True)
class AppConfig:
    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    playback: PlaybackConfig = field(default_factory=PlaybackConfig)
    codex: CodexConfig = field(default_factory=CodexConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    config_path: str | None = None


_TOP_KEYS = {
    "audio",
    "stt",
    "tts",
    "playback",
    "codex",
    "permissions",
    "session",
    "metrics",
    "paths",
}


def default_config_path() -> Path | None:
    cwd_file = Path.cwd() / "config.yaml"
    if cwd_file.exists():
        return cwd_file
    home_file = Path.home() / ".codex-voice-orb" / "config.yaml"
    if home_file.exists():
        return home_file
    return None


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is not None:
        file = Path(path).expanduser()
        if not file.is_file():
            raise ConfigError(f"config file not found: {file}")
        config = _from_mapping(_read_yaml(file))
        return replace(config, config_path=str(file))
    found = default_config_path()
    if found is None:
        return AppConfig()
    config = _from_mapping(_read_yaml(found))
    return replace(config, config_path=str(found))


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError("config root must be a mapping")
    return loaded


def _from_mapping(data: dict[str, Any]) -> AppConfig:
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise ConfigError("unknown config keys: " + ", ".join(unknown))
    audio = _audio(_section(data, "audio"))
    stt = _stt(_section(data, "stt"))
    tts = _tts(_section(data, "tts"))
    playback = _playback(_section(data, "playback"))
    codex = _codex(_section(data, "codex"))
    permissions = _permissions(_section(data, "permissions"))
    session = _session(_section(data, "session"))
    metrics = _metrics(_section(data, "metrics"))
    paths = _paths(_section(data, "paths"))
    config = AppConfig(
        audio=audio,
        stt=stt,
        tts=tts,
        playback=playback,
        codex=codex,
        permissions=permissions,
        session=session,
        metrics=metrics,
        paths=paths,
    )
    _validate(config)
    return config


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    raw = data.get(name)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{name} must be a mapping")
    return raw


def _unknown(raw: dict[str, Any], allowed: set[str], section: str) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        raise ConfigError(f"{section}: unknown keys {', '.join(extra)}")


def _bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _int(raw: dict[str, Any], key: str, default: int) -> int:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _optional_int(raw: dict[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be an integer")
    return value


def _float(raw: dict[str, Any], key: str, default: float) -> float:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    return float(value)


def _str(raw: dict[str, Any], key: str, default: str) -> str:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    if key not in raw or raw[key] is None:
        return None
    value = raw[key]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    return value


def _strings(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    if key not in raw or raw[key] is None:
        return ()
    value = raw[key]
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{key} must be a list of strings")
    return tuple(value)


def _choice(value: str, allowed: set[str], key: str) -> str:
    if value not in allowed:
        raise ConfigError(f"{key} must be one of {', '.join(sorted(allowed))}")
    return value


def _audio(raw: dict[str, Any]) -> AudioConfig:
    _unknown(
        raw,
        {
            "mode",
            "input_device",
            "sample_rate",
            "channels",
            "hotkey",
            "confirm_key",
            "min_utterance_ms",
            "gate_during_playback",
            "vad",
        },
        "audio",
    )
    vad_raw = raw.get("vad") or {}
    if not isinstance(vad_raw, dict):
        raise ConfigError("audio.vad must be a mapping")
    _unknown(vad_raw, {"threshold", "start_ms", "end_ms"}, "audio.vad")
    device: str | int | None
    if "input_device" not in raw or raw["input_device"] is None:
        device = None
    elif isinstance(raw["input_device"], bool):
        raise ConfigError("audio.input_device must be a device name or index")
    elif isinstance(raw["input_device"], int):
        device = raw["input_device"]
    elif isinstance(raw["input_device"], str):
        device = raw["input_device"]
    else:
        raise ConfigError("audio.input_device must be a device name or index")
    return AudioConfig(
        mode=_choice(_str(raw, "mode", "ptt"), {"ptt", "vad"}, "audio.mode"),
        input_device=device,
        sample_rate=_int(raw, "sample_rate", 16000),
        channels=_int(raw, "channels", 1),
        hotkey=_str(raw, "hotkey", "ctrl+space"),
        confirm_key=_str(raw, "confirm_key", "ctrl+shift+y"),
        min_utterance_ms=_int(raw, "min_utterance_ms", 250),
        gate_during_playback=_bool(raw, "gate_during_playback", False),
        vad=VadConfig(
            threshold=_float(vad_raw, "threshold", 0.02),
            start_ms=_int(vad_raw, "start_ms", 200),
            end_ms=_int(vad_raw, "end_ms", 700),
        ),
    )


def _stt(raw: dict[str, Any]) -> SttConfig:
    _unknown(
        raw,
        {"engine", "model", "model_path", "language", "device", "compute_type", "whisper_cpp_bin"},
        "stt",
    )
    return SttConfig(
        engine=_choice(_str(raw, "engine", "faster-whisper"), {"faster-whisper", "whisper.cpp"}, "stt.engine"),
        model=_str(raw, "model", "small"),
        model_path=_expand(_opt_str(raw, "model_path")),
        language=_str(raw, "language", "auto"),
        device=_str(raw, "device", "cpu"),
        compute_type=_str(raw, "compute_type", "int8"),
        whisper_cpp_bin=_str(raw, "whisper_cpp_bin", "whisper-cli"),
    )


def _tts(raw: dict[str, Any]) -> TtsConfig:
    _unknown(raw, {"engine", "voice", "rate", "max_chars", "max_sentences", "ack", "output_dir"}, "tts")
    output = _str(raw, "output_dir", "~/.codex-voice-orb/tts")
    return TtsConfig(
        engine=_str(raw, "engine", "say"),
        voice=_opt_str(raw, "voice"),
        rate=_optional_int(raw, "rate"),
        max_chars=_int(raw, "max_chars", 280),
        max_sentences=_int(raw, "max_sentences", 2),
        ack=_str(raw, "ack", "好。"),
        output_dir=str(Path(output).expanduser()),
    )


def _playback(raw: dict[str, Any]) -> PlaybackConfig:
    _unknown(raw, {"player"}, "playback")
    return PlaybackConfig(player=_choice(_str(raw, "player", "auto"), {"auto", "afplay", "aplay", "ffplay"}, "playback.player"))


def _codex(raw: dict[str, Any]) -> CodexConfig:
    _unknown(
        raw,
        {
            "bin",
            "sandbox",
            "model",
            "config_overrides",
            "images",
            "cwd",
            "extra_args",
            "timeout_s",
            "resume",
            "voice_preamble",
            "skip_git_repo_check",
        },
        "codex",
    )
    return CodexConfig(
        bin=_str(raw, "bin", "codex"),
        sandbox=_str(raw, "sandbox", "read-only"),
        model=_opt_str(raw, "model"),
        config_overrides=_strings(raw, "config_overrides"),
        images=_strings(raw, "images"),
        cwd=_expand(_opt_str(raw, "cwd")),
        extra_args=_strings(raw, "extra_args"),
        timeout_s=_float(raw, "timeout_s", 180.0),
        resume=_choice(_str(raw, "resume", "id"), {"id", "last"}, "codex.resume"),
        voice_preamble=_str(raw, "voice_preamble", CodexConfig.voice_preamble),
        skip_git_repo_check=_bool(raw, "skip_git_repo_check", True),
    )


def _permissions(raw: dict[str, Any]) -> PermissionsConfig:
    _unknown(
        raw,
        {
            "confirm_timeout_s",
            "allow_full_auto",
            "allow_danger_full_access",
            "confirm_prompt",
            "cancel_message",
            "timeout_message",
            "unclear_message",
            "blocked_message",
        },
        "permissions",
    )
    defaults = PermissionsConfig()
    return PermissionsConfig(
        confirm_timeout_s=_float(raw, "confirm_timeout_s", defaults.confirm_timeout_s),
        allow_full_auto=_bool(raw, "allow_full_auto", False),
        allow_danger_full_access=_bool(raw, "allow_danger_full_access", False),
        confirm_prompt=_str(raw, "confirm_prompt", defaults.confirm_prompt),
        cancel_message=_str(raw, "cancel_message", defaults.cancel_message),
        timeout_message=_str(raw, "timeout_message", defaults.timeout_message),
        unclear_message=_str(raw, "unclear_message", defaults.unclear_message),
        blocked_message=_str(raw, "blocked_message", defaults.blocked_message),
    )


def _session(raw: dict[str, Any]) -> SessionConfig:
    _unknown(raw, {"path", "bind_cwd"}, "session")
    path = _str(raw, "path", "~/.codex-voice-orb/session.json")
    return SessionConfig(path=str(Path(path).expanduser()), bind_cwd=_bool(raw, "bind_cwd", True))


def _metrics(raw: dict[str, Any]) -> MetricsConfig:
    _unknown(raw, {"path"}, "metrics")
    path = _str(raw, "path", "~/.codex-voice-orb/metrics.jsonl")
    return MetricsConfig(path=str(Path(path).expanduser()))


def _paths(raw: dict[str, Any]) -> PathsConfig:
    _unknown(raw, {"audio_dir", "delete_audio_after_stt"}, "paths")
    audio_dir = _str(raw, "audio_dir", "~/.codex-voice-orb/audio")
    return PathsConfig(
        audio_dir=str(Path(audio_dir).expanduser()),
        delete_audio_after_stt=_bool(raw, "delete_audio_after_stt", True),
    )


def _validate(config: AppConfig) -> None:
    if config.audio.sample_rate <= 0:
        raise ConfigError("audio.sample_rate must be positive")
    if config.audio.channels != 1:
        raise ConfigError("audio.channels must be 1 in v0.1")
    if config.audio.vad.threshold < 0:
        raise ConfigError("audio.vad.threshold must be >= 0")
    if config.audio.vad.start_ms < 0 or config.audio.vad.end_ms < 0:
        raise ConfigError("VAD windows must be >= 0")
    if config.codex.timeout_s <= 0:
        raise ConfigError("codex.timeout_s must be positive")
    if config.permissions.confirm_timeout_s <= 0:
        raise ConfigError("permissions.confirm_timeout_s must be positive")
    if config.tts.max_chars <= 0 or config.tts.max_sentences <= 0:
        raise ConfigError("tts limits must be positive")
    reason = unsafe_config_reason(
        extra_args=config.codex.extra_args,
        config_overrides=config.codex.config_overrides,
        sandbox=config.codex.sandbox,
        allow_full_auto=config.permissions.allow_full_auto,
        allow_danger_full_access=config.permissions.allow_danger_full_access,
    )
    if reason:
        raise ConfigError(reason)
