"""Command line: doctor, one text turn, and the push-to-talk loop."""

from __future__ import annotations

import argparse
import logging
import queue
import sys

from orb import __version__
from orb.audio import AudioDependencyError, AudioInput
from orb.backends.codex_cli import CodexCliBackend
from orb.config import AppConfig, ConfigError, load_config
from orb.doctor import doctor_exit_code, render_report, run_doctor
from orb.hotkey import HotkeyDependencyError, HotkeyListener
from orb.metrics import MetricsLog
from orb.orchestrator import Orchestrator
from orb.playback import GatedPlayback, PlaybackController
from orb.session import SessionStore
from orb.stt import WhisperSTT
from orb.tts import NoTTS, SayTTS


def _common_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        "-c",
        default=argparse.SUPPRESS,
        help="config.yaml 路径。缺省时读取 ./config.yaml 或 ~/.codex-voice-orb/config.yaml",
    )
    common.add_argument("--verbose", "-v", action="store_true", default=argparse.SUPPRESS, help="打印调试日志")
    return common


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orb",
        description="按住说话，把语音转成文字交给本机 Codex CLI，再朗读一句摘要。",
        parents=[_common_parser()],
    )
    parser.add_argument("--version", action="version", version=f"codex-voice-orb {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", parents=[_common_parser()], help="检查麦克风、Codex、登录状态、语音识别模型和 TTS")

    once = sub.add_parser("once", parents=[_common_parser()], help="把一段文字交给 Codex，并朗读摘要")
    once.add_argument("--text", "-t", required=True, help="要发送的文字")
    once.add_argument("--image", action="append", default=[], help="附加图片，可重复。对应 codex exec --image")
    once.add_argument("--no-tts", action="store_true", help="只打印回复，不调用 say")

    listen = sub.add_parser("listen", parents=[_common_parser()], help="按住热键说话，或用 VAD 分段")
    listen.add_argument("--stdin", action="store_true", help="从标准输入读文字，用来先验证多轮 resume")
    listen.add_argument("--no-tts", action="store_true", help="只打印回复，不调用 say")

    session = sub.add_parser("session", parents=[_common_parser()], help="查看或清除 Codex 会话")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_sub.add_parser("show", help="打印当前 thread id")
    session_sub.add_parser("clear", help="忘记 thread id，下一轮重新开始")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.command:
        parser.print_help()
        return 2
    try:
        config = load_config(getattr(args, "config", None))
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    if args.command == "doctor":
        checks = run_doctor(config)
        sys.stdout.write(render_report(checks))
        return doctor_exit_code(checks)
    if args.command == "session":
        return _session_command(config, args)
    speak = not getattr(args, "no_tts", False)
    runtime = build_runtime(config, speak=speak)
    if args.command == "once":
        return _once(runtime, args.text, args.image)
    if args.command == "listen":
        if args.stdin:
            return _listen_stdin(runtime)
        return _listen_audio(runtime)
    parser.print_help()
    return 2


def build_runtime(config: AppConfig, *, speak: bool = True, backend=None, stt=None, tts=None, playback=None):
    store = SessionStore(config.session.path)
    agent = backend if backend is not None else CodexCliBackend(config, session=store)
    speech = stt if stt is not None else WhisperSTT(config)
    voice = tts if tts is not None else (SayTTS(config) if speak else NoTTS())
    player = playback if playback is not None else PlaybackController(config)
    orchestrator = Orchestrator(
        config,
        agent,
        speech,
        voice,
        player,
        MetricsLog(config.metrics.path),
    )
    return type("Runtime", (), {"config": config, "orchestrator": orchestrator, "session": store})()


def _once(runtime, text: str, images: list[str]) -> int:
    result = runtime.orchestrator.submit_text(text, images=images)
    if result.status == "awaiting_confirmation":
        print("请输入确认（确认 / 取消）：", flush=True)
        try:
            answer = input()
        except EOFError:
            answer = ""
        result = runtime.orchestrator.submit_text(answer)
    return 0 if result.status == "ok" else 1


def _listen_stdin(runtime) -> int:
    print("输入文字后回车。空行退出。高风险操作会再问一次确认。", flush=True)
    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            return 0
        runtime.orchestrator.submit_text(line)
        runtime.orchestrator.poll()


def _listen_audio(runtime) -> int:
    config = runtime.config
    events: queue.Queue = queue.Queue()

    def on_event(event) -> None:
        if event.kind == "speech_start":
            runtime.orchestrator.on_speech_start()
        events.put(event)

    audio = AudioInput(config, on_event)
    try:
        audio.start()
    except AudioDependencyError as exc:
        print(exc, file=sys.stderr)
        return 1
    if config.audio.gate_during_playback:
        runtime.orchestrator.playback = GatedPlayback(runtime.orchestrator.playback, audio, True)
    hotkeys = None
    try:
        if config.audio.mode == "ptt":
            hotkeys = HotkeyListener(
                config.audio.hotkey,
                config.audio.confirm_key,
                on_ptt=audio.set_ptt,
                on_confirm=lambda: events.put(_confirm_event()),
            )
            hotkeys.start()
        else:
            try:
                hotkeys = HotkeyListener(
                    config.audio.hotkey,
                    config.audio.confirm_key,
                    on_ptt=lambda _held: None,
                    on_confirm=lambda: events.put(_confirm_event()),
                )
                hotkeys.start()
            except HotkeyDependencyError as exc:
                print(f"确认键不可用：{exc}", file=sys.stderr)
                hotkeys = None
    except HotkeyDependencyError as exc:
        print(exc, file=sys.stderr)
        audio.stop()
        return 1
    if config.audio.mode == "ptt":
        print(f"正在听。按住 {config.audio.hotkey} 说话，松开后发送。确认键是 {config.audio.confirm_key}。Ctrl+C 退出。", flush=True)
    else:
        print("正在听（VAD）。说完停顿后会发送。Ctrl+C 退出。", flush=True)
    try:
        while True:
            try:
                event = events.get(timeout=0.2)
            except queue.Empty:
                runtime.orchestrator.poll()
                continue
            if event.kind == "confirm_key":
                runtime.orchestrator.confirm_with_key()
            elif event.kind == "speech_end" and event.wav_path:
                runtime.orchestrator.handle_wav(event.wav_path)
            runtime.orchestrator.poll()
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        audio.stop()
        if hotkeys is not None:
            hotkeys.stop()


def _session_command(config: AppConfig, args) -> int:
    store = SessionStore(config.session.path)
    action = _session_action(args)
    if action == "clear":
        removed = store.clear()
        print("已清除会话。" if removed else "没有已保存的会话。")
        return 0
    state = store.load()
    if state is None:
        print(f"没有已保存的会话（{config.session.path}）。")
        return 0
    print(f"thread_id: {state.thread_id}")
    print(f"cwd: {state.cwd or ''}")
    print(f"updated_at: {state.updated_at}")
    return 0


def _session_action(args) -> str:
    return getattr(args, "session_command", None) or "show"


def _confirm_event():
    from orb.audio import SpeechEvent
    import time

    return SpeechEvent(kind="confirm_key", monotonic_ns=time.monotonic_ns(), wav_path=None)
