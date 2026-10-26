import pytest

from orb.permissions import (
    classify_confirmation,
    classify_risk,
    is_high_risk_command,
    is_high_risk_tool,
)

HIGH = [
    "把这些临时文件删掉",
    "请删除缓存",
    "deploy the service",
    "git push origin main",
    "帮我发邮件给老板",
    "merge the pull request",
    "buy a domain",
    "rm -rf /tmp/build",
    "用 full-auto 跑",
    "格式化磁盘",
]

LOW = [
    "现在几点",
    "这个仓库里有什么文件",
    "用两句话解释这段代码",
    "hello",
    "把结论说得更短一点",
]


@pytest.mark.parametrize("text", HIGH)
def test_high_risk_phrases(text: str):
    assert classify_risk(text) == "high"


@pytest.mark.parametrize("text", LOW)
def test_low_risk_phrases(text: str):
    assert classify_risk(text) == "low"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("确认", "yes"),
        ("确认。", "yes"),
        ("好的", "other"),
        ("好", "other"),
        ("嗯", "other"),
        ("可以", "other"),
        ("YES", "yes"),
        ("ok", "other"),
        ("yeah", "other"),
        ("取消", "no"),
        ("不要", "no"),
        ("no", "no"),
        ("确认一下再删", "other"),
        ("好的，顺便再查一下天气", "other"),
        ("maybe", "other"),
    ],
)
def test_confirmation_is_exact(text: str, expected: str):
    assert classify_confirmation(text) == expected


def test_shell_commands():
    assert is_high_risk_command("rm -rf /tmp/x")
    assert is_high_risk_command("git push")
    assert is_high_risk_command("echo ok; rm build")
    assert not is_high_risk_command("ls -la")
    assert not is_high_risk_command("echo rm is a word")
    assert is_high_risk_tool("mail", "send_message")
    assert not is_high_risk_tool("github", "search_issues")
