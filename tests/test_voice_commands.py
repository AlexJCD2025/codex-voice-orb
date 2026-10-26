from orb.voice_commands import RESUME_PROMPT, match_voice_command


def test_stop_background_and_resume_commands():
    assert match_voice_command("停").kind == "stop"
    assert match_voice_command("停下吧").kind == "stop"
    assert match_voice_command("取消").kind == "stop"
    assert match_voice_command("Stop").kind == "stop"
    assert match_voice_command("后台做").kind == "background"
    assert match_voice_command("先这样。").kind == "background"
    assert match_voice_command("放到后台").kind == "background"
    resume = match_voice_command("接着上次聊")
    assert resume.kind == "resume" and resume.remainder == ""
    cont = match_voice_command("继续，那个数字是多少？")
    assert cont.kind == "resume"
    assert cont.remainder == "那个数字是多少"
    assert match_voice_command("请继续").remainder == ""
    assert RESUME_PROMPT


def test_longer_sentences_are_not_commands():
    assert match_voice_command("不要接着上次聊") is None
    assert match_voice_command("别继续") is None
    assert match_voice_command("停车去哪") is None
    assert match_voice_command("用两句话说明当前目录是做什么的") is None
    assert match_voice_command("先这样把临时文件删掉") is None
