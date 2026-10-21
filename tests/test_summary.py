from orb.summary import spoken_summary, strip_markdown


def test_strip_markdown_and_keep_two_sentences():
    text = "这是口语结论。后面还有一句。\n\n```python\nprint('hi')\n```\n详见 [文档](https://example.com)。"
    assert "print" not in strip_markdown(text)
    summary = spoken_summary(text, max_chars=80, max_sentences=2)
    assert summary.startswith("这是口语结论。")
    assert "后面还有一句。" in summary
    assert "print" not in summary
    assert "example.com" not in summary


def test_explicit_spoken_line_wins():
    text = "摘要：先看日志。\n\n然后是很长的细节。"
    assert spoken_summary(text) == "先看日志。"


def test_summary_trims():
    summary = spoken_summary("甲" * 50, max_chars=10, max_sentences=1)
    assert len(summary) == 10
    assert summary.endswith("…")
