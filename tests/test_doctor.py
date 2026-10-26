import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from orb.config import AppConfig
from orb.doctor import check_codex, check_microphone, check_stt, check_tts, render_report, run_doctor

ROOT = Path(__file__).resolve().parents[1]


def test_probes_cover_mic_codex_and_auth(tmp_path):
    calls = []

    def run(argv, timeout):
        calls.append(list(argv))
        if argv[-1] == "--version":
            return 0, "codex-cli 0.153.4\n", ""
        if argv[-2:] == ["login", "status"]:
            return 0, "Logged in using ChatGPT\n", ""
        return 1, "", "unexpected"

    probes = {
        "which": lambda name: "/opt/homebrew/bin/codex" if name == "codex" else None,
        "run": run,
        "list_inputs": lambda: [{"index": 1, "name": "USB PnP Audio", "channels": 1, "default": True}],
    }
    config = replace(AppConfig(), audio=replace(AppConfig().audio, input_device="USB"))
    checks = {check.name: check for check in run_doctor(config, probes)}
    assert checks["microphone"].status == "pass"
    assert "USB PnP Audio" in checks["microphone"].detail
    assert checks["codex"].status == "pass"
    assert "0.153.4" in checks["codex"].detail
    assert checks["codex-auth"].status == "pass"
    assert checks["safety"].status == "pass"
    assert calls[0][-1] == "--version"
    assert calls[1][-2:] == ["login", "status"]
    report = render_report(list(checks.values()))
    assert "codex-voice-orb doctor" in report


def test_missing_device_and_binary():
    probes = {
        "which": lambda name: None,
        "run": lambda argv, timeout: (1, "", "missing"),
        "list_inputs": lambda: [{"index": 0, "name": "Built-in", "channels": 1, "default": True}],
    }
    config = replace(AppConfig(), audio=replace(AppConfig().audio, input_device="Wireless GO"))
    checks = {check.name: check for check in run_doctor(config, probes)}
    assert checks["microphone"].status == "fail"
    assert checks["codex"].status == "fail"
    assert checks["codex-auth"].status == "fail"


def test_codex_absolute_path(tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return 0, "codex-cli 0.153.4\n", ""

    check, found = check_codex(replace(AppConfig(), codex=replace(AppConfig().codex, bin=str(binary))), {"run": run, "which": lambda name: None})
    assert check.status == "pass"
    assert found == str(binary)
    assert calls[0][0] == str(binary)


def test_stt_and_tts_report(monkeypatch, tmp_path):
    missing = check_stt(AppConfig())
    assert missing.status in {"fail", "warn", "pass"}
    if missing.status == "fail":
        assert "faster-whisper" in missing.detail
    cpp = replace(
        AppConfig(),
        stt=replace(AppConfig().stt, engine="whisper.cpp", whisper_cpp_bin="whisper-cli-missing", model_path=str(tmp_path / "no.bin")),
    )
    assert check_stt(cpp).status == "fail"
    model = tmp_path / "ggml.bin"
    model.write_bytes(b"x")
    binary = tmp_path / "whisper-cli"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    ready = replace(cpp, stt=replace(cpp.stt, whisper_cpp_bin=str(binary), model_path=str(model)))
    assert check_stt(ready).status == "pass"
    monkeypatch.setattr("orb.doctor.shutil.which", lambda name: "/usr/bin/say" if name == "say" else None)
    assert check_tts(AppConfig()).status == "pass"
    monkeypatch.setattr("orb.doctor.shutil.which", lambda name: None)
    assert check_tts(AppConfig()).status == "fail"


def test_microphone_probe_error_is_a_failed_check():
    probes = {"list_inputs": lambda: (_ for _ in ()).throw(RuntimeError("sounddevice is not installed")), "which": lambda name: None, "run": lambda argv, timeout: (1, "", "")}
    checks = {check.name: check for check in run_doctor(AppConfig(), probes)}
    assert checks["microphone"].status == "fail"
    assert "sounddevice" in checks["microphone"].detail


def test_doctor_command_runs_on_this_machine():
    completed = subprocess.run(
        [sys.executable, "-m", "orb", "doctor"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in {0, 1}
    assert "Traceback" not in completed.stderr
    for name in ("microphone", "codex", "stt", "tts"):
        assert name in completed.stdout
