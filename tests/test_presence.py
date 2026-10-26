from orb.config import PresenceConfig
from orb.presence import Presence, TurnActivity, activity_label, progress_phrase


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _presence(**kwargs):
    clock = Clock()
    config = PresenceConfig(**kwargs)
    activity = TurnActivity(clock)
    presence = Presence(config, activity, clock)
    presence.start()
    return clock, activity, presence


def test_filler_then_progress_intervals_double_to_the_cap():
    clock, activity, presence = _presence(stuck_after_s=0)
    activity.note("在看文件", 0)
    assert presence.tick(quiet=True) is None
    clock.now = 2
    assert presence.tick(quiet=True) == "我看一下。"
    clock.now = 31
    assert presence.tick(quiet=True) is None
    clock.now = 32
    first = presence.tick(quiet=True)
    assert first == "还在处理，在看文件。"
    clock.now = 91
    assert presence.tick(quiet=True) is None
    clock.now = 92
    assert "在看文件" in presence.tick(quiet=True)
    clock.now = 211
    assert presence.tick(quiet=True) is None
    clock.now = 212
    later = presence.tick(quiet=True)
    assert "分钟" in later and "在看文件" in later


def test_stuck_waits_for_silence_and_ignores_a_running_tool():
    clock, activity, presence = _presence(filler="", progress_start_s=0, stuck_after_s=10)
    clock.now = 9
    assert presence.tick(quiet=True) is None
    clock.now = 10
    assert "卡住" in presence.tick(quiet=True)
    clock.now = 40
    assert presence.tick(quiet=True) is None
    activity.note("在跑命令", 1)
    clock.now = 200
    assert presence.tick(quiet=True) is None
    activity.note("", 0)
    clock.now = 209
    assert presence.tick(quiet=True) is None
    clock.now = 210
    assert "卡住" in presence.tick(quiet=True)


def test_quiet_false_and_disabled_say_nothing():
    clock, _activity, presence = _presence(filler_delay_s=0)
    clock.now = 5
    assert presence.tick(quiet=False) is None
    assert presence.tick(quiet=True) == "我看一下。"
    _clock, _activity, off = _presence(enabled=False, filler_delay_s=0)
    _clock.now = 5
    assert off.tick(quiet=True) is None


def test_activity_labels_for_read_and_write_commands():
    read = "/bin/zsh -lc \"pwd && rg --files -g 'README*' | head -30\""
    assert activity_label("command_execution", read) == "在看文件"
    assert activity_label("command_execution", "sed -n '1,20p' README.md") == "在看文件"
    assert activity_label("command_execution", "rm -rf /tmp/secret") == "在跑命令"
    assert activity_label("mcp_tool_call") == "在调用工具"
    assert "在看文件" in progress_phrase("在看文件", 10)
    assert "分钟" in progress_phrase("在跑命令", 90)
