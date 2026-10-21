import json
import os
from dataclasses import replace

import pytest

from orb.backends.base import CodexCancelled, CodexExecError, HighRiskActionBlocked
from orb.backends.codex_cli import CodexCliBackend
from orb.codex_safety import UnsafeCodexInvocation
from orb.config import AppConfig
from orb.session import SessionStore

ROOT = os.path.dirname(os.path.dirname(__file__))
FAKE = os.path.join(ROOT, "tests", "fakes", "fake_codex.py")


def _config(tmp_path, **codex_kwargs):
    base = AppConfig()
    codex = replace(base.codex, **codex_kwargs)
    session = replace(base.session, path=str(tmp_path / "session.json"), bind_cwd=True)
    permissions = base.permissions
    if codex_kwargs.get("sandbox") == "danger-full-access":
        permissions = replace(base.permissions, allow_danger_full_access=True)
    if "--yolo" in codex.extra_args or "--full-auto" in codex.extra_args:
        permissions = replace(permissions, allow_full_auto=True)
    return replace(base, codex=codex, session=session, permissions=permissions)


def _events(*items):
    return [json.dumps(item, ensure_ascii=False) + "\n" for item in items]


def _runner(lines, calls):
    def run(argv, stdin, on_line, cancel, timeout):
        calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        for line in lines:
            if cancel is not None and cancel.is_set():
                return -15, ""
            on_line(line)
            if cancel is not None and cancel.is_set():
                return -15, ""
        return 0, ""

    return run


def _message(text, item_type="agent_message", legacy=False):
    item = {"id": "item_1", "text": text}
    if legacy:
        item["item_type"] = item_type
    else:
        item["type"] = item_type
    return {"type": "item.completed", "item": item}


def test_first_turn_starts_a_sandboxed_exec(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "thread-abc"},
        _message("先说结论。"),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=SessionStore(tmp_path / "session.json"))
    result = backend.ask("仓库里有什么")
    argv = calls[0]["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "resume" not in argv
    assert argv[-1] == "-"
    assert "--yolo" not in argv
    assert "--full-auto" not in argv
    assert "--approve-for-me" not in argv
    assert "danger-full-access" not in argv
    assert "用户说：仓库里有什么" in calls[0]["stdin"]
    assert result.text == "先说结论。"
    assert result.session_id == "thread-abc"
    assert backend.session.load().thread_id == "thread-abc"


def test_second_turn_resumes_thread_id(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "thread-abc"}, _message("第二句。"))
    store = SessionStore(tmp_path / "session.json")
    store.save("thread-abc", os.getcwd())
    backend = CodexCliBackend(_config(tmp_path, resume="id"), runner=_runner(lines, calls), session=store)
    backend.ask("继续")
    argv = calls[0]["argv"]
    assert argv[argv.index("resume") + 1] == "thread-abc"
    assert "--last" not in argv


def test_resume_last_flag(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "thread-last"}, _message("好。"))
    backend = CodexCliBackend(_config(tmp_path, resume="last"), runner=_runner(lines, calls))
    backend.ask("继续")
    argv = calls[0]["argv"]
    assert argv[argv.index("resume") + 1] == "--last"


def test_model_config_and_image_flags(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "t"}, _message("好。"))
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    config = _config(
        tmp_path,
        sandbox="workspace-write",
        model="gpt-test",
        config_overrides=("foo=1",),
        images=(str(image),),
        cwd=str(tmp_path),
    )
    backend = CodexCliBackend(config, runner=_runner(lines, calls))
    backend.ask("看图", images=["/tmp/other.png"])
    argv = calls[0]["argv"]
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert argv[argv.index("-c") + 1] == "foo=1"
    assert argv.index("-m") < argv.index("-c")
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    images = [argv[index + 1] for index, token in enumerate(argv) if token == "--image"]
    assert images == [str(image), "/tmp/other.png"]
    assert "--skip-git-repo-check" in argv


def test_legacy_assistant_message_and_last_message_wins(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "t"},
        _message("第一段", legacy=True),
        _message("最终结论。"),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    result = backend.ask("hi")
    assert result.text == "最终结论。"
    assert "第一段" in result.full_text


def test_high_risk_command_is_blocked_and_session_is_kept(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "thread-risk"},
        {
            "type": "item.started",
            "item": {"id": "c1", "type": "command_execution", "command": "rm -rf /tmp/secret", "status": "in_progress"},
        },
        _message("删好了"),
    )
    store = SessionStore(tmp_path / "session.json")
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=store)
    with pytest.raises(HighRiskActionBlocked):
        backend.ask("看看目录", confirmed=False)
    assert store.load().thread_id == "thread-risk"


def test_confirmed_turn_allows_the_same_command(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.started", "item": {"id": "c1", "type": "command_execution", "command": "rm -rf build"}},
        _message("好了。"),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    result = backend.ask("删掉构建目录", confirmed=True)
    assert result.text == "好了。"


def test_file_delete_and_mcp_tool_are_blocked(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {"id": "f1", "type": "file_change", "changes": [{"path": "notes.md", "kind": "delete"}]},
        },
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    with pytest.raises(HighRiskActionBlocked, match="notes.md"):
        backend.ask("整理笔记", confirmed=False)

    calls.clear()
    lines = _events(
        {"type": "thread.started", "thread_id": "t2"},
        {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "mail", "tool": "send_message"}},
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    with pytest.raises(HighRiskActionBlocked, match="send_message"):
        backend.ask("写一封草稿", confirmed=False)


def test_yolo_and_danger_require_opt_in_and_confirmation(tmp_path):
    calls = []
    config = AppConfig()
    config = replace(
        config,
        codex=replace(config.codex, extra_args=("--yolo",)),
        session=replace(config.session, path=str(tmp_path / "session.json")),
    )
    backend = CodexCliBackend(config, runner=_runner([], calls))
    assert backend.always_requires_confirmation() is True
    with pytest.raises(UnsafeCodexInvocation):
        backend.ask("hi", confirmed=True)
    assert calls == []

    opted = replace(config, permissions=replace(config.permissions, allow_full_auto=True))
    backend = CodexCliBackend(opted, runner=_runner(_events({"type": "thread.started", "thread_id": "t"}, _message("好。")), calls))
    with pytest.raises(UnsafeCodexInvocation):
        backend.ask("hi", confirmed=False)
    backend.ask("hi", confirmed=True)
    assert "--yolo" in calls[-1]["argv"]

    danger = replace(
        AppConfig(),
        codex=replace(AppConfig().codex, sandbox="danger-full-access"),
        permissions=replace(AppConfig().permissions, allow_danger_full_access=True),
        session=replace(AppConfig().session, path=str(tmp_path / "danger.json")),
    )
    danger_calls = []
    danger_backend = CodexCliBackend(
        danger,
        runner=_runner(_events({"type": "thread.started", "thread_id": "d"}, _message("好。")), danger_calls),
    )
    assert danger_backend.always_requires_confirmation() is True
    with pytest.raises(UnsafeCodexInvocation):
        danger_backend.ask("hi", confirmed=False)
    danger_backend.ask("hi", confirmed=True)
    assert danger_calls[-1]["argv"][danger_calls[-1]["argv"].index("-s") + 1] == "danger-full-access"


def test_cwd_change_starts_a_new_thread(tmp_path):
    calls = []
    store = SessionStore(tmp_path / "session.json")
    store.save("old-thread", "/somewhere/else")
    lines = _events({"type": "thread.started", "thread_id": "new-thread"}, _message("好。"))
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=store)
    backend.ask("hi")
    assert "resume" not in calls[0]["argv"]
    assert store.load().thread_id == "new-thread"


def test_turn_failure_and_nonzero_exit(tmp_path):
    calls = []

    def fail_runner(argv, stdin, on_line, cancel, timeout):
        calls.append(argv)
        on_line(json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n")
        on_line(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}) + "\n")
        return 0, ""

    backend = CodexCliBackend(_config(tmp_path), runner=fail_runner)
    with pytest.raises(CodexExecError, match="boom"):
        backend.ask("hi")

    def exit_runner(argv, stdin, on_line, cancel, timeout):
        return 2, "not logged in"

    backend = CodexCliBackend(_config(tmp_path), runner=exit_runner)
    with pytest.raises(CodexExecError, match="not logged in"):
        backend.ask("hi")


def test_cancel_before_start(tmp_path):
    from orb.backends.base import CancelToken

    token = CancelToken()
    token.cancel()
    backend = CodexCliBackend(_config(tmp_path), runner=_runner([], []))
    with pytest.raises(CodexCancelled):
        backend.ask("hi", cancel=token)


def test_fake_codex_script_round_trip(tmp_path, monkeypatch):
    os.chmod(FAKE, 0o755)
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    config = _config(tmp_path, bin=FAKE, model="gpt-test", sandbox="workspace-write")
    backend = CodexCliBackend(config, session=SessionStore(tmp_path / "session.json"))
    result = backend.ask("第一句")
    assert result.text == "这是口语结论。已经处理完。"
    assert result.session_id == "thread-new"
    second = backend.ask("第二句")
    assert second.session_id == "thread-new"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "resume" not in rows[0]["argv"]
    assert rows[1]["argv"][rows[1]["argv"].index("resume") + 1] == "thread-new"
    assert "用户说：第二句" in rows[1]["stdin"]
    assert "--full-auto" not in rows[0]["argv"]
    assert "gpt-test" in rows[0]["argv"]
