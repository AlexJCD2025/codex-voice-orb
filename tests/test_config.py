from dataclasses import replace
from pathlib import Path

import pytest

from orb.config import AppConfig, ConfigError, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads_safe_defaults():
    config = load_config(ROOT / "config.example.yaml")
    assert config.codex.bin == "/opt/homebrew/bin/codex"
    assert config.codex.sandbox == "read-only"
    assert config.codex.resume == "id"
    assert config.codex.skip_git_repo_check is True
    assert config.audio.mode == "ptt"
    assert config.audio.hotkey == "ctrl+space"
    assert config.stt.engine == "faster-whisper"
    assert config.stt.language == "auto"
    assert config.tts.engine == "say"
    assert config.permissions.allow_full_auto is False
    assert config.permissions.allow_danger_full_access is False
    assert "口语" in config.codex.voice_preamble
    assert config.session.path.endswith(".codex-voice-orb/session.json")


def test_workspace_write_is_allowed(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("codex:\n  sandbox: workspace-write\n", encoding="utf-8")
    config = load_config(path)
    assert config.codex.sandbox == "workspace-write"


def test_unknown_key_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("codex:\n  sandbox: read-only\n  yolo: true\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(path)


def test_danger_sandbox_is_rejected_by_default(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("codex:\n  sandbox: danger-full-access\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="allow_danger_full_access"):
        load_config(path)


def test_full_auto_flag_is_rejected_unless_opted_in(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("codex:\n  extra_args: ['--full-auto']\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="full-auto"):
        load_config(path)
    path.write_text(
        "codex:\n  extra_args: ['--yolo']\npermissions:\n  allow_full_auto: true\n",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.permissions.allow_full_auto is True
    assert loaded.codex.extra_args == ("--yolo",)


def test_missing_file(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")


def test_builtin_defaults_do_not_enable_full_auto():
    config = AppConfig()
    assert config.codex.sandbox == "read-only"
    assert config.permissions.allow_full_auto is False
    assert config.codex.extra_args == ()


def test_stereo_is_rejected(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("audio:\n  channels: 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="channels"):
        load_config(path)


def test_replace_keeps_nested_defaults():
    config = replace(AppConfig(), codex=replace(AppConfig().codex, model="gpt-test"))
    assert config.codex.model == "gpt-test"
    assert config.audio.mode == "ptt"
