"""The execute process must not start until confirmation. JSONL is not the gate."""

import json
import os
from dataclasses import replace
from pathlib import Path

from orb.backends.codex_cli import CodexCliBackend
from orb.config import AppConfig
from orb.metrics import MetricsLog
from orb.orchestrator import Orchestrator
from orb.session import SessionStore

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "tests" / "fakes" / "fake_codex.py"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


class Silent:
    def synthesize(self, text, *, turn_id):
        return ""


class Playback:
    def arm(self):
        return None

    def play(self, path):
        return None

    def stop(self):
        return None


def _runtime(tmp_path, monkeypatch):
    os.chmod(FAKE, 0o755)
    log_path = tmp_path / "calls.jsonl"
    marker = tmp_path / "side-effect.txt"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setenv("FAKE_CODEX_SIDE_EFFECT", str(marker))
    config = replace(
        AppConfig(),
        codex=replace(AppConfig().codex, bin=str(FAKE), sandbox="workspace-write", model="gpt-test"),
        permissions=replace(AppConfig().permissions, confirm_timeout_s=10),
        session=replace(AppConfig().session, path=str(tmp_path / "session.json")),
        metrics=replace(AppConfig().metrics, path=str(tmp_path / "metrics.jsonl")),
        tts=replace(AppConfig().tts, ack="好。", output_dir=str(tmp_path / "tts")),
    )
    backend = CodexCliBackend(config, session=SessionStore(config.session.path))
    orchestrator = Orchestrator(
        config,
        backend,
        None,
        Silent(),
        Playback(),
        MetricsLog(config.metrics.path),
    )
    return orchestrator, log_path, marker


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_side_effect_event_does_not_start_execute(tmp_path, monkeypatch):
    orchestrator, log_path, marker = _runtime(tmp_path, monkeypatch)
    pending = orchestrator.submit_text("把临时文件删掉")
    assert pending.status == "awaiting_confirmation"
    rows = _rows(log_path)
    assert len(rows) == 1
    assert rows[0]["argv"][rows[0]["argv"].index("-s") + 1] == "read-only"
    assert "workspace-write" not in rows[0]["argv"]
    assert "--last" not in rows[0]["argv"]
    assert marker.read_text(encoding="utf-8") == "side effect before event\n"
    assert "zsh" in rows[0]["stdin"] or "删" in rows[0]["stdin"]

    ignored = orchestrator.submit_text("确认")
    assert ignored.status == "awaiting_confirmation"
    for word in ("好", "嗯", "可以", "好的"):
        assert orchestrator.submit_text(word).status == "awaiting_confirmation"
    assert len(_rows(log_path)) == 1

    confirmed = orchestrator.confirm_with_key()
    assert confirmed.status == "ok"
    rows = _rows(log_path)
    assert len(rows) == 2
    execute = rows[1]["argv"]
    assert execute[execute.index("-s") + 1] == "workspace-write"
    assert "用户已确认" in rows[1]["stdin"]
    assert "删除临时文件" in rows[1]["stdin"]
    assert "--last" not in execute


def test_confirmation_after_deadline_does_not_execute(tmp_path, monkeypatch):
    orchestrator, log_path, _marker = _runtime(tmp_path, monkeypatch)
    clock = Clock()
    orchestrator.clock = clock
    orchestrator.timer = clock
    pending = orchestrator.submit_text("把临时文件删掉")
    assert pending.status == "awaiting_confirmation"
    clock.advance(10)
    late_key = orchestrator.confirm_with_key()
    assert late_key.status == "timeout"
    assert len(_rows(log_path)) == 1
    assert orchestrator.submit_text("确认").status == "ignored"
    assert len(_rows(log_path)) == 1


def test_voice_yes_can_confirm_a_non_destructive_write(tmp_path, monkeypatch):
    orchestrator, log_path, marker = _runtime(tmp_path, monkeypatch)
    pending = orchestrator.submit_text("写入备注")
    assert pending.status == "awaiting_confirmation"
    assert len(_rows(log_path)) == 1
    assert not marker.exists()
    assert orchestrator.submit_text("好的").status == "cancelled"
    assert len(_rows(log_path)) == 1
    orchestrator.submit_text("写入备注")
    confirmed = orchestrator.submit_text("确认")
    assert confirmed.status == "ok"
    rows = _rows(log_path)
    assert len(rows) == 3
    assert rows[2]["argv"][rows[2]["argv"].index("-s") + 1] == "workspace-write"
    assert "用户已确认" in rows[2]["stdin"]
