#!/usr/bin/env python3
"""Stand-in for `codex exec` used by unit tests.

Command events are written only after a pretend side effect, matching the real
CLI: the JSONL line is not what prevented the action.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


UNSAFE = {
    "--yolo",
    "--full-auto",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--not-so-yolo",
}


def resume_thread(argv: list[str]) -> str | None:
    if "resume" not in argv:
        return None
    tokens = argv[argv.index("resume") + 1 :]
    for token in tokens:
        if token == "--last":
            return "thread-last"
        if token.startswith("-"):
            continue
        return token
    return None


def user_utterance(prompt: str) -> str:
    marker = "用户说："
    if marker not in prompt:
        return prompt
    return prompt.split(marker, 1)[1]


def main() -> int:
    argv = sys.argv[1:]
    names = {arg.split("=", 1)[0] for arg in argv}
    if UNSAFE.intersection(argv) or UNSAFE.intersection(names) or "danger-full-access" in argv or "--last" in argv:
        sys.stderr.write("refusing unsafe flags\n")
        return 3
    if "exec" not in argv:
        sys.stderr.write("expected exec\n")
        return 2
    prompt = sys.stdin.read()
    log_path = os.environ.get("FAKE_CODEX_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "stdin": prompt}, ensure_ascii=False) + "\n")
    thread = resume_thread(argv) or "thread-new"
    events: list[dict[str, object]] = [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
    ]
    utterance = user_utterance(prompt)
    if "用户已确认" in prompt:
        message = "这是口语结论。已经处理完。"
    elif "规划阶段" in prompt and any(word in utterance for word in ("删", "delete", "部署", "付款", "发邮件")):
        marker = os.environ.get("FAKE_CODEX_SIDE_EFFECT")
        if marker:
            Path(marker).write_text("side effect before event\n", encoding="utf-8")
        events.append(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd1",
                    "type": "command_execution",
                    "command": "/bin/zsh -lc 'rm -rf /tmp/secret'",
                    "status": "completed",
                },
            }
        )
        message = json.dumps(
            {
                "spoken": "将删除临时文件。",
                "execute": True,
                "actions": [{"kind": "delete", "summary": "删除临时文件", "risk": "high"}],
            },
            ensure_ascii=False,
        )
    elif "规划阶段" in prompt and "写入备注" in utterance:
        message = json.dumps(
            {
                "spoken": "将写入备注。",
                "execute": True,
                "actions": [{"kind": "write", "summary": "写入备注", "risk": "low"}],
            },
            ensure_ascii=False,
        )
    elif "规划阶段" in prompt:
        message = json.dumps(
            {"spoken": "这是口语结论。已经处理完。", "execute": False, "actions": []},
            ensure_ascii=False,
        )
    else:
        message = "这是口语结论。已经处理完。"
    events.append({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": message}})
    events.append(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 8, "reasoning_output_tokens": 0},
        }
    )
    for event in events:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
