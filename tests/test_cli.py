import io
import json
import os
from pathlib import Path

import pytest
import yaml

from orb.cli import main

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fakes" / "fake_codex.py"


def _config(tmp_path: Path) -> Path:
    os.chmod(FAKE, 0o755)
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    data = {
        "codex": {
            "bin": str(FAKE),
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "config_overrides": ["sandbox_workspace_write.network_access=false"],
            "images": [str(image)],
            "resume": "id",
            "skip_git_repo_check": True,
            "voice_preamble": "先说结论。",
            "timeout_s": 30,
        },
        "permissions": {"confirm_timeout_s": 0.2},
        "session": {"path": str(tmp_path / "session.json")},
        "metrics": {"path": str(tmp_path / "metrics.jsonl")},
        "tts": {"output_dir": str(tmp_path / "tts")},
        "paths": {"audio_dir": str(tmp_path / "audio")},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_once_resumes_and_session_commands(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    log_path = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    image = tmp_path / "pic.png"
    assert main(["--config", str(config), "once", "--text", "第一句", "--no-tts", "--image", str(image)]) == 0
    first_out = capsys.readouterr().out
    assert "这是口语结论" in first_out
    assert "朗读：" in first_out
    assert main(["once", "--config", str(config), "--text", "第二句", "--no-tts"]) == 0
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    first = rows[0]["argv"]
    assert first[first.index("-s") + 1] == "read-only"
    assert "gpt-test" in first
    assert "sandbox_workspace_write.network_access=false" in first
    assert "--json" in first
    assert str(image) in first
    assert "--yolo" not in first
    assert "--full-auto" not in first
    assert "danger-full-access" not in first
    assert "用户说：第一句" in rows[0]["stdin"]
    second = rows[1]["argv"]
    assert second[second.index("resume") + 1] == "thread-new"
    assert main(["--config", str(config), "session", "show"]) == 0
    assert "thread-new" in capsys.readouterr().out
    assert main(["--config", str(config), "session", "clear"]) == 0
    assert main(["--config", str(config), "session", "show"]) == 0
    assert "没有已保存的会话" in capsys.readouterr().out
    metrics = json.loads((tmp_path / "metrics.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "codex_ms" in metrics
    assert "第一句" not in json.dumps(metrics)


def test_high_risk_once_plans_then_rejects_typed_yes_for_delete(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    log_path = tmp_path / "risk.jsonl"
    marker = tmp_path / "side-effect.txt"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setenv("FAKE_CODEX_SIDE_EFFECT", str(marker))
    monkeypatch.setattr("sys.stdin", io.StringIO("确认\n"))
    assert main(["--config", str(config), "once", "--text", "把临时文件删掉", "--no-tts"]) == 1
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["argv"][rows[0]["argv"].index("-s") + 1] == "read-only"
    assert "删掉" in rows[0]["stdin"]
    assert marker.exists()
    output = capsys.readouterr().out
    assert "确认键" in output
    assert "用户已确认" not in output


def test_cancelled_high_risk_does_not_execute(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    log_path = tmp_path / "cancel.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("取消\n"))
    assert main(["--config", str(config), "once", "--text", "把临时文件删掉", "--no-tts"]) == 1
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["argv"][rows[0]["argv"].index("-s") + 1] == "read-only"
    assert "用户已确认" not in rows[0]["stdin"]
    assert "已取消" in capsys.readouterr().out


def test_typed_yes_executes_a_write_after_the_plan(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    log_path = tmp_path / "write.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("确认\n"))
    assert main(["--config", str(config), "once", "--text", "写入备注", "--no-tts"]) == 0
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert rows[0]["argv"][rows[0]["argv"].index("-s") + 1] == "read-only"
    assert rows[1]["argv"][rows[1]["argv"].index("-s") + 1] == "workspace-write"
    assert "用户已确认" in rows[1]["stdin"]
    assert "写入备注" in rows[1]["stdin"]
    assert capsys.readouterr().out


def test_stdin_listen_keeps_the_thread(tmp_path, monkeypatch, capsys):
    config = _config(tmp_path)
    log_path = tmp_path / "stdin.jsonl"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setattr("sys.stdin", io.StringIO("记住数字四十二\n我刚才说了什么\n\n"))
    assert main(["--config", str(config), "listen", "--stdin", "--no-tts"]) == 0
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert "resume" not in rows[0]["argv"]
    assert rows[1]["argv"][rows[1]["argv"].index("resume") + 1] == "thread-new"
    assert capsys.readouterr().out


def test_bad_config_and_help(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("codex:\n  sandbox: danger-full-access\n", encoding="utf-8")
    assert main(["--config", str(bad), "doctor"]) == 2
    assert "配置错误" in capsys.readouterr().err
    assert main([]) == 2


def test_version():
    with pytest.raises(SystemExit) as caught:
        main(["--version"])
    assert caught.value.code == 0
