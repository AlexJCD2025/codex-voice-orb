import json
import os
import sys
import time
from dataclasses import replace

import pytest

from orb.backends.base import CancelToken, CodexCancelled, CodexExecError, CodexTimeout
from orb.backends.codex_cli import CodexCliBackend, subprocess_runner
from orb.codex_safety import UnsafeCodexInvocation, assert_argv_safe
from orb.config import AppConfig
from orb.plan import PlannedAction
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
    return replace(base, codex=codex, session=session, permissions=permissions)


def _events(*items):
    return [json.dumps(item, ensure_ascii=False) + "\n" for item in items]


def _completed():
    return {"type": "turn.completed"}


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


def test_plan_turn_is_read_only_and_saves_after_completion(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "thread-abc"},
        _message("先说结论。"),
        _completed(),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=SessionStore(tmp_path / "session.json"))
    result = backend.plan("仓库里有什么")
    argv = calls[0]["argv"]
    assert argv[:2] == ["codex", "exec"]
    assert "--json" in argv
    assert "--color" in argv and argv[argv.index("--color") + 1] == "never"
    assert argv[argv.index("-s") + 1] == "read-only"
    assert "--output-schema" in argv
    assert "resume" not in argv
    assert "--last" not in argv
    assert argv[-1] == "-"
    assert "--yolo" not in argv
    assert "--full-auto" not in argv
    assert "--approve-for-me" not in argv
    assert "danger-full-access" not in argv
    assert "规划阶段" in calls[0]["stdin"]
    assert "用户说：仓库里有什么" in calls[0]["stdin"]
    assert result.text == "先说结论。"
    assert result.session_id == "thread-abc"
    assert backend.session.load().thread_id == "thread-abc"


def test_second_turn_resumes_saved_thread_id(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "thread-abc"}, _message("第二句。"), _completed())
    store = SessionStore(tmp_path / "session.json")
    store.save("thread-abc", os.getcwd())
    backend = CodexCliBackend(_config(tmp_path, resume="id"), runner=_runner(lines, calls), session=store)
    backend.plan("继续")
    argv = calls[0]["argv"]
    assert argv[argv.index("resume") + 1] == "thread-abc"
    assert "--last" not in argv


def test_resume_last_is_never_emitted(tmp_path):
    calls = []
    backend = CodexCliBackend(_config(tmp_path, resume="last"), runner=_runner([], calls))
    with pytest.raises(UnsafeCodexInvocation, match="--last"):
        backend.plan("继续")
    assert calls == []


def test_plan_ignores_configured_sandbox_and_execute_uses_it(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "t"}, _message("好。"), _completed())
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    config = _config(
        tmp_path,
        sandbox="workspace-write",
        model="gpt-test",
        config_overrides=("sandbox_workspace_write.network_access=false",),
        images=(str(image),),
        cwd=str(tmp_path),
    )
    backend = CodexCliBackend(config, runner=_runner(lines, calls))
    backend.plan("看图", images=["/tmp/other.png"])
    argv = calls[0]["argv"]
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-test"
    assert "sandbox_workspace_write.network_access=false" in argv
    assert argv[argv.index("-C") + 1] == str(tmp_path)
    images = [argv[index + 1] for index, token in enumerate(argv) if token == "--image"]
    assert images == [str(image), "/tmp/other.png"]
    assert "--skip-git-repo-check" in argv
    action = PlannedAction("write", "写文件", "low")
    backend.execute("看图", [action], images=["/tmp/other.png"], resume_id="t")
    execute_argv = calls[1]["argv"]
    assert execute_argv[execute_argv.index("-s") + 1] == "workspace-write"
    assert execute_argv[execute_argv.index("resume") + 1] == "t"
    assert "--image" not in execute_argv
    assert "--output-schema" not in execute_argv
    assert "用户已确认" in calls[1]["stdin"]
    assert "write: 写文件" in calls[1]["stdin"]


def test_legacy_assistant_message_and_last_message_wins(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "t"},
        _message("第一段", legacy=True),
        _message("最终结论。"),
        _completed(),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    result = backend.plan("hi")
    assert result.text == "最终结论。"
    assert "第一段" in result.raw_text


def test_command_event_does_not_block_the_plan_turn(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "thread-risk"},
        {
            "type": "item.completed",
            "item": {
                "id": "c1",
                "type": "command_execution",
                "command": "/bin/zsh -lc 'rm -rf /tmp/secret'",
                "status": "completed",
            },
        },
        _message('{"spoken":"将删除。","execute":true,"actions":[{"kind":"delete","summary":"删除临时文件","risk":"high"}]}'),
        _completed(),
    )
    store = SessionStore(tmp_path / "session.json")
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=store)
    result = backend.plan("看看目录")
    assert result.side_effects == ("/bin/zsh -lc 'rm -rf /tmp/secret'",)
    assert result.plan.execute is True
    assert result.plan.actions[0].kind == "delete"
    assert store.load().thread_id == "thread-risk"
    assert len(calls) == 1
    assert calls[0]["argv"][calls[0]["argv"].index("-s") + 1] == "read-only"


def test_file_and_mcp_events_are_recorded_not_raised(tmp_path):
    calls = []
    lines = _events(
        {"type": "thread.started", "thread_id": "t"},
        {
            "type": "item.completed",
            "item": {"id": "f1", "type": "file_change", "changes": [{"path": "notes.md", "kind": "delete"}]},
        },
        _message("只是看看。"),
        _completed(),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    result = backend.plan("整理笔记")
    assert result.side_effects == ("file_change delete notes.md",)

    calls.clear()
    lines = _events(
        {"type": "thread.started", "thread_id": "t2"},
        {"type": "item.started", "item": {"id": "m1", "type": "mcp_tool_call", "server": "mail", "tool": "send_message"}},
        _message("草稿。"),
        _completed(),
    )
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls))
    result = backend.plan("写一封草稿")
    assert result.side_effects == ("mail send_message",)


def test_yolo_and_open_flags_are_always_rejected(tmp_path):
    yolo = ["codex", "exec", "--json", "--color", "never", "-s", "read-only", "--yolo", "-"]
    with pytest.raises(UnsafeCodexInvocation):
        assert_argv_safe(yolo, confirmed=True, allow_full_auto=True, allow_danger_full_access=True)
    bypass = ["codex", "exec", "--json", "--color", "never", "-s", "read-only", "--dangerously-bypass-approvals-and-sandbox", "-"]
    with pytest.raises(UnsafeCodexInvocation):
        assert_argv_safe(bypass, confirmed=True, allow_full_auto=True, allow_danger_full_access=True)
    never = ["codex", "exec", "--json", "--color", "never", "-s", "read-only", '--config=approval_policy="never"', "-"]
    with pytest.raises(UnsafeCodexInvocation, match="approval_policy"):
        assert_argv_safe(never, confirmed=True, allow_full_auto=True, allow_danger_full_access=True)
    added = ["codex", "exec", "--json", "--color", "never", "-s", "read-only", "--add-dir", "/", "-"]
    with pytest.raises(UnsafeCodexInvocation, match="add-dir"):
        assert_argv_safe(added, confirmed=True, allow_full_auto=True, allow_danger_full_access=True)
    danger = ["codex", "exec", "--json", "--color", "never", "--sandbox=danger-full-access", "-"]
    with pytest.raises(UnsafeCodexInvocation, match="danger-full-access"):
        assert_argv_safe(danger, confirmed=False, allow_full_auto=False, allow_danger_full_access=True)
    with pytest.raises(UnsafeCodexInvocation, match="danger-full-access"):
        assert_argv_safe(danger, confirmed=True, allow_full_auto=True, allow_danger_full_access=False)
    assert_argv_safe(danger, confirmed=True, allow_full_auto=False, allow_danger_full_access=True)

    calls = []
    config = replace(
        AppConfig(),
        codex=replace(AppConfig().codex, extra_args=("--yolo", "--add-dir", "/")),
        session=replace(AppConfig().session, path=str(tmp_path / "session.json")),
    )
    backend = CodexCliBackend(
        config,
        runner=_runner(_events({"type": "thread.started", "thread_id": "t"}, _message("好。"), _completed()), calls),
    )
    backend.plan("hi")
    assert "--yolo" not in calls[0]["argv"]
    assert "--add-dir" not in calls[0]["argv"]


def test_danger_sandbox_stays_off_the_plan_turn(tmp_path):
    calls = []
    lines = _events({"type": "thread.started", "thread_id": "d"}, _message("好。"), _completed())
    danger = _config(tmp_path, sandbox="danger-full-access")
    backend = CodexCliBackend(danger, runner=_runner(lines, calls))
    assert backend.always_requires_confirmation() is True
    backend.plan("hi")
    assert calls[0]["argv"][calls[0]["argv"].index("-s") + 1] == "read-only"
    backend.execute("hi", [PlannedAction("command", "列出文件", "low")], resume_id="d")
    assert calls[1]["argv"][calls[1]["argv"].index("-s") + 1] == "danger-full-access"


def test_thread_is_not_saved_without_successful_completion(tmp_path):
    store = SessionStore(tmp_path / "session.json")

    def fail_runner(argv, stdin, on_line, cancel, timeout):
        on_line(json.dumps({"type": "thread.started", "thread_id": "t"}) + "\n")
        on_line(json.dumps({"type": "turn.failed", "error": {"message": "boom"}}) + "\n")
        return 0, ""

    backend = CodexCliBackend(_config(tmp_path), runner=fail_runner, session=store)
    with pytest.raises(CodexExecError, match="boom"):
        backend.plan("hi")
    assert store.load() is None

    def exit_runner(argv, stdin, on_line, cancel, timeout):
        on_line(json.dumps({"type": "thread.started", "thread_id": "t2"}) + "\n")
        on_line(json.dumps(_message("半截")) + "\n")
        return 2, "not logged in"

    backend = CodexCliBackend(_config(tmp_path), runner=exit_runner, session=store)
    with pytest.raises(CodexExecError, match="not logged in"):
        backend.plan("hi")
    assert store.load() is None


def test_cwd_change_starts_a_new_thread(tmp_path):
    calls = []
    store = SessionStore(tmp_path / "session.json")
    store.save("old-thread", "/somewhere/else")
    lines = _events({"type": "thread.started", "thread_id": "new-thread"}, _message("好。"), _completed())
    backend = CodexCliBackend(_config(tmp_path), runner=_runner(lines, calls), session=store)
    backend.plan("hi")
    assert "resume" not in calls[0]["argv"]
    assert store.load().thread_id == "new-thread"


def test_cancel_before_start(tmp_path):
    token = CancelToken()
    token.cancel()
    backend = CodexCliBackend(_config(tmp_path), runner=_runner([], []))
    with pytest.raises(CodexCancelled):
        backend.plan("hi", cancel=token)


def test_kill_escalates_when_sigterm_is_ignored(tmp_path):
    script = tmp_path / "ignore_term.py"
    script.write_text(
        "import signal, time, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('hi\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with pytest.raises(CodexTimeout):
        subprocess_runner([sys.executable, str(script)], "", lambda _line: None, None, 0.3, kill_grace=0.3)
    assert time.monotonic() - started < 5


def test_fake_codex_script_round_trip(tmp_path, monkeypatch):
    os.chmod(FAKE, 0o755)
    log_path = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    config = _config(tmp_path, bin=FAKE, model="gpt-test", sandbox="workspace-write")
    backend = CodexCliBackend(config, session=SessionStore(tmp_path / "session.json"))
    result = backend.plan("第一句")
    assert result.text == "这是口语结论。已经处理完。"
    assert result.plan.execute is False
    assert result.session_id == "thread-new"
    second = backend.plan("第二句")
    assert second.session_id == "thread-new"
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["argv"][rows[0]["argv"].index("-s") + 1] == "read-only"
    assert "resume" not in rows[0]["argv"]
    assert "--last" not in rows[1]["argv"]
    assert rows[1]["argv"][rows[1]["argv"].index("resume") + 1] == "thread-new"
    assert "用户说：第二句" in rows[1]["stdin"]
    assert "--full-auto" not in rows[0]["argv"]
    assert "gpt-test" in rows[0]["argv"]
