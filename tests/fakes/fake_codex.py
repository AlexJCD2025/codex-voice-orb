#!/usr/bin/env python3
"""Stand-in for `codex exec` used by unit tests.

It speaks the JSONL event shape of `codex exec --json` and records argv plus stdin.
"""

from __future__ import annotations

import json
import os
import sys


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


def main() -> int:
    argv = sys.argv[1:]
    unsafe = {"--yolo", "--full-auto", "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me"}
    if unsafe.intersection(argv) or "danger-full-access" in argv:
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
    message = "这是口语结论。已经处理完。"
    events = [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": message}},
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 8, "reasoning_output_tokens": 0},
        },
    ]
    for event in events:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
