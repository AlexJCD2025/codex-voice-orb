"""Local preflight: microphone, Codex binary and login, STT model, TTS."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orb.config import AppConfig
from orb.playback import resolve_player


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def render_report(checks: list[Check]) -> str:
    lines = ["codex-voice-orb doctor"]
    for check in checks:
        lines.append(f"[{check.status}] {check.name}: {check.detail}")
    passed = sum(check.status == "pass" for check in checks)
    warned = sum(check.status == "warn" for check in checks)
    failed = sum(check.status == "fail" for check in checks)
    lines.append(f"{passed} passed, {warned} warnings, {failed} failed")
    return "\n".join(lines) + "\n"


def doctor_exit_code(checks: list[Check]) -> int:
    return 1 if any(check.status == "fail" for check in checks) else 0


def run_doctor(config: AppConfig, probes: dict | None = None, *, hotkey: bool = False) -> list[Check]:
    probes = probes or default_probes()
    codex_check, codex_bin = _guard("codex", lambda: check_codex(config, probes))
    checks = [
        _guard("safety", lambda: check_safety(config)),
        _guard("microphone", lambda: check_microphone(config, probes)),
        codex_check,
        _guard("codex-auth", lambda: check_codex_auth(codex_bin, probes)),
        _guard("stt", lambda: check_stt(config)),
        _guard("tts", lambda: check_tts(config)),
        _guard("playback", lambda: check_playback(config)),
        _guard("hotkey", lambda: check_hotkey(config, interactive=hotkey)),
    ]
    return checks


def default_probes() -> dict:
    return {
        "list_inputs": _default_list_inputs,
        "check_input_settings": _default_check_input,
        "which": shutil.which,
        "run": _default_run,
    }


def check_safety(config: AppConfig) -> Check:
    if config.permissions.allow_danger_full_access or config.codex.sandbox == "danger-full-access":
        return Check(
            "safety",
            "warn",
            "danger-full-access is opted in; the plan turn stays read-only and execute waits for the confirm key",
        )
    if config.codex.sandbox not in {"read-only", "workspace-write"}:
        return Check("safety", "fail", f"sandbox {config.codex.sandbox} is not a default sandbox")
    return Check(
        "safety",
        "pass",
        f"plan sandbox read-only; execute sandbox {config.codex.sandbox}; yolo is refused",
    )


def check_microphone(config: AppConfig, probes: dict) -> Check:
    try:
        devices = probes["list_inputs"]()
    except Exception as exc:
        return Check("microphone", "fail", str(exc))
    if not devices:
        return Check("microphone", "fail", "no input devices")
    wanted = config.audio.input_device
    names = ", ".join(str(device.get("name")) for device in devices[:8])
    if wanted is None:
        result = Check("microphone", "pass", f"default input available; devices: {names}")
    else:
        wanted_text = str(wanted)
        result = None
        for device in devices:
            name = str(device.get("name") or "")
            if str(device.get("index")) == wanted_text or wanted_text.casefold() in name.casefold():
                result = Check("microphone", "pass", f"found {name} (index {device.get('index')})")
                break
        if result is None:
            return Check("microphone", "fail", f"input device {wanted_text} not found; have: {names}")
    checker = probes.get("check_input_settings")
    if checker is None or result.status != "pass":
        return result
    try:
        detail = checker(config)
    except Exception as exc:
        return Check("microphone", "fail", f"input settings: {exc}")
    if detail:
        return Check("microphone", "pass", result.detail + "; " + str(detail))
    return result


def check_codex(config: AppConfig, probes: dict) -> tuple[Check, str | None]:
    binary = resolve_executable(config.codex.bin, probes["which"])
    if not binary:
        return Check("codex", "fail", f"not found: {config.codex.bin}"), None
    try:
        code, out, err = probes["run"]([binary, "--version"], 10)
    except subprocess.TimeoutExpired:
        return Check("codex", "fail", "codex --version timed out"), binary
    except Exception as exc:
        return Check("codex", "fail", str(exc)), binary
    if code != 0:
        detail = (err or out or "codex --version failed").strip()
        return Check("codex", "fail", detail[:500]), binary
    version_text = (out or err).strip()
    version = version_text.splitlines()[0] if version_text else "unknown version"
    detail = f"{binary}: {version}"
    status = "pass"
    if Path(binary).name != "codex":
        status = "warn"
        detail = f"{binary}: {version}; basename is not codex"
    return Check("codex", status, detail), binary


def check_codex_auth(binary: str | None, probes: dict) -> Check:
    if not binary:
        return Check("codex-auth", "fail", "skipped because codex is missing")
    try:
        code, out, err = probes["run"]([binary, "login", "status"], 15)
    except subprocess.TimeoutExpired:
        return Check("codex-auth", "fail", "codex login status timed out")
    except Exception as exc:
        return Check("codex-auth", "fail", str(exc))
    detail = (out or err).strip()
    text = detail.splitlines()[0] if detail else f"exit {code}"
    if code == 0:
        return Check("codex-auth", "pass", text[:500])
    return Check("codex-auth", "fail", text[:500])


def check_stt(config: AppConfig) -> Check:
    if config.stt.engine == "whisper.cpp":
        binary = resolve_executable(config.stt.whisper_cpp_bin, shutil.which)
        if not binary:
            return Check("stt", "fail", f"whisper.cpp binary not found: {config.stt.whisper_cpp_bin}")
        model = config.stt.model_path or config.stt.model
        if not model or not Path(model).expanduser().is_file():
            return Check("stt", "fail", f"whisper.cpp model file not found: {model}")
        return Check("stt", "pass", f"{binary} model {model} language {config.stt.language}")
    if importlib.util.find_spec("faster_whisper") is None:
        return Check("stt", "fail", "faster-whisper is not installed (pip install -e '.[stt]')")
    if config.stt.model_path:
        if Path(config.stt.model_path).expanduser().exists():
            return Check("stt", "pass", f"faster-whisper model path {config.stt.model_path}")
        return Check("stt", "fail", f"model path not found: {config.stt.model_path}")
    cached = huggingface_model_dir(config.stt.model)
    if cached is not None:
        return Check("stt", "pass", f"faster-whisper model {config.stt.model} cached at {cached}")
    return Check(
        "stt",
        "warn",
        f"faster-whisper installed; model {config.stt.model} downloads on first transcription (Chinese and English, language {config.stt.language})",
    )


def check_tts(config: AppConfig) -> Check:
    if config.tts.engine != "say":
        return Check("tts", "fail", f"unsupported tts engine {config.tts.engine}; v0.1 speaks with macOS say")
    path = shutil.which("say")
    if not path:
        return Check("tts", "fail", "macOS say not found; it ships with macOS and is the default TTS")
    detail = path
    if config.tts.voice:
        detail = f"{path} voice {config.tts.voice}"
    return Check("tts", "pass", detail)


def check_hotkey(config: AppConfig, *, interactive: bool = False) -> Check:
    detail = (
        f"hotkey {config.audio.hotkey} is not proof of permission. "
        "Starting a listener does not mean macOS delivered the keys. "
        "Check Input Monitoring, Accessibility, and Secure Keyboard Entry. "
        "Pass --hotkey to wait five seconds for one key."
    )
    if not interactive:
        return Check("hotkey", "warn", detail)
    try:
        from pynput import keyboard
    except ImportError:
        return Check("hotkey", "fail", "pynput is not installed; cannot test Input Monitoring")
    import threading

    got = threading.Event()

    def on_press(_key):
        got.set()
        return False

    listener = keyboard.Listener(on_press=on_press)
    listener.start()
    try:
        ok = got.wait(5)
    finally:
        listener.stop()
    if ok:
        return Check("hotkey", "pass", "received one key")
    return Check("hotkey", "warn", "no key in 5s. " + detail)


def check_playback(config: AppConfig) -> Check:
    try:
        command = resolve_player(config.playback.player)
    except Exception as exc:
        return Check("playback", "warn", f"{exc}; replies still print as text")
    binary = command[0]
    if shutil.which(binary) is None:
        return Check("playback", "warn", f"{binary} not found; replies still print as text")
    return Check("playback", "pass", " ".join(command))


def resolve_executable(name: str, which) -> str | None:
    path = Path(name).expanduser()
    if path.is_absolute() or name.startswith("."):
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    found = which(name)
    return found if isinstance(found, str) else None


def huggingface_model_dir(model: str) -> Path | None:
    root = Path.home() / ".cache" / "huggingface" / "hub"
    if not root.is_dir():
        return None
    slug = model.replace("/", "--")
    candidates = [
        root / f"models--Systran--faster-whisper-{slug}",
        root / f"models--{slug}",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    for child in root.iterdir():
        if child.is_dir() and child.name.endswith(f"faster-whisper-{slug}"):
            return child
    return None


def _default_list_inputs():
    from orb.audio import list_input_devices

    return list_input_devices()


def _default_check_input(config: AppConfig) -> str:
    import sounddevice as sd

    device = config.audio.input_device
    info = sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
    rate = int(round(float(info["default_samplerate"])))
    sd.check_input_settings(device=device, channels=1, dtype="float32", samplerate=rate)
    return f"default rate {rate}"


def _default_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _guard(name: str, function):
    try:
        return function()
    except Exception as exc:
        failed = Check(name, "fail", f"{type(exc).__name__}: {exc}")
        if name == "codex":
            return failed, None
        return failed
