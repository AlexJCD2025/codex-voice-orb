"""Closed argv checks for Codex. Free extra flags are rejected."""

from __future__ import annotations

FORBIDDEN_FLAGS = (
    "--full-auto",
    "--yolo",
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust",
    "--approve-for-me",
    "--not-so-yolo",
)

ALLOWED_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")

_VALUE_FLAGS = {
    "-s",
    "--sandbox",
    "-m",
    "-c",
    "-C",
    "--image",
    "--output-schema",
}

_ALLOWED_OVERRIDE_KEYS = {"model", "model_reasoning_effort"}


class UnsafeCodexInvocation(RuntimeError):
    """Raised when an argv would bypass the sandbox or approval gate."""


def sandbox_values(argv: list[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-s", "--sandbox") and index + 1 < len(argv):
            values.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith("--sandbox="):
            values.append(arg.split("=", 1)[1])
        index += 1
    return values


def config_override_values(argv: list[str]) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-c", "--config") and index + 1 < len(argv):
            values.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith("--config=") or arg.startswith("-c="):
            values.append(arg.split("=", 1)[1])
        index += 1
    return values


def override_sets_never(override: str) -> bool:
    compact = override.replace(" ", "").lower()
    return "approval_policy" in compact and "never" in compact


def override_sets_danger(override: str) -> bool:
    compact = override.replace(" ", "").lower()
    return "danger-full-access" in compact or "danger_full_access" in compact


def _unquote(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def override_allowed(override: str) -> bool:
    if override_sets_never(override) or override_sets_danger(override):
        return False
    if "=" not in override:
        return False
    key, value = override.split("=", 1)
    key = key.strip()
    value = _unquote(value)
    if not value or "\n" in value or value.startswith("-"):
        return False
    if key in _ALLOWED_OVERRIDE_KEYS:
        return True
    if key == "sandbox_workspace_write.network_access":
        return value.lower() in {"false", "0"}
    return False


def _flag_name(arg: str) -> str:
    if arg.startswith("--") and "=" in arg:
        return arg.split("=", 1)[0]
    return arg


def assert_argv_safe(
    argv: list[str],
    *,
    confirmed: bool,
    allow_full_auto: bool,
    allow_danger_full_access: bool,
) -> None:
    """Reject bypass flags, open-ended args, and an unconfirmed danger sandbox.

    `allow_full_auto` is accepted so older callers still compile. It does not
    unlock anything: --yolo and the bypass flags are always refused.
    """
    del allow_full_auto
    problems = _closed_argv_problems(argv)
    for value in sandbox_values(argv):
        if value == "danger-full-access" and not (confirmed and allow_danger_full_access):
            problems.append("danger-full-access")
    if problems:
        raise UnsafeCodexInvocation("refusing unsafe codex invocation: " + ", ".join(problems))


def _closed_argv_problems(argv: list[str]) -> list[str]:
    problems: list[str] = []
    if len(argv) < 3 or argv[1] != "exec":
        return ["argv must be <bin> exec ... -"]
    rest = argv[2:]
    for arg in rest:
        name = _flag_name(arg)
        if arg in FORBIDDEN_FLAGS or name in FORBIDDEN_FLAGS:
            problems.append(arg)
        if name == "--add-dir":
            problems.append(arg)
        if name == "--last" or arg == "--last":
            problems.append("--last")
    index = 0
    saw_stdin = False
    sandbox_seen = False
    while index < len(rest):
        arg = rest[index]
        name = _flag_name(arg)
        if arg in FORBIDDEN_FLAGS or name in FORBIDDEN_FLAGS or name == "--add-dir" or arg == "--last":
            index += 2 if arg in _VALUE_FLAGS and index + 1 < len(rest) else 1
            continue
        if arg == "-" and index == len(rest) - 1:
            saw_stdin = True
            index += 1
            continue
        if arg == "--json" or arg == "--skip-git-repo-check":
            index += 1
            continue
        if arg == "--color":
            if index + 1 >= len(rest) or rest[index + 1] != "never":
                problems.append("--color")
                index += 1
            else:
                index += 2
            continue
        if arg in ("-s", "--sandbox"):
            if index + 1 >= len(rest):
                problems.append(arg)
                break
            sandbox_seen = True
            if rest[index + 1] not in ALLOWED_SANDBOXES:
                problems.append("sandbox " + rest[index + 1])
            index += 2
            continue
        if arg.startswith("--sandbox="):
            sandbox_seen = True
            value = arg.split("=", 1)[1]
            if value not in ALLOWED_SANDBOXES:
                problems.append("sandbox " + value)
            index += 1
            continue
        if arg in ("-m", "-C", "--image", "--output-schema"):
            if index + 1 >= len(rest) or rest[index + 1].startswith("-"):
                problems.append(arg)
                break
            index += 2
            continue
        if arg in ("-c", "--config"):
            if index + 1 >= len(rest):
                problems.append(arg)
                break
            if not override_allowed(rest[index + 1]):
                problems.append(rest[index + 1])
            index += 2
            continue
        if arg.startswith("--config="):
            value = arg.split("=", 1)[1]
            if not override_allowed(value):
                problems.append(arg)
            index += 1
            continue
        if arg == "resume":
            if index + 1 >= len(rest) or rest[index + 1].startswith("-"):
                problems.append("resume requires a saved thread id")
                break
            index += 2
            continue
        problems.append(arg)
        index += 1
    if not saw_stdin:
        problems.append("missing stdin marker")
    if not sandbox_seen:
        problems.append("missing sandbox")
    return problems


def unsafe_config_reason(
    *,
    extra_args: tuple[str, ...],
    config_overrides: tuple[str, ...],
    sandbox: str,
    allow_full_auto: bool,
    allow_danger_full_access: bool,
) -> str | None:
    """Return a config-time error when the file asks for a bypass or an open argv."""
    if extra_args:
        return "codex.extra_args is not allowed; the Codex argv is closed"
    if allow_full_auto:
        return "permissions.allow_full_auto is not supported; --yolo is always refused"
    if sandbox not in ALLOWED_SANDBOXES:
        return f"codex.sandbox must be one of {', '.join(ALLOWED_SANDBOXES)}"
    if sandbox == "danger-full-access" and not allow_danger_full_access:
        return "sandbox danger-full-access requires permissions.allow_danger_full_access: true"
    for override in config_overrides:
        if not override_allowed(override):
            return f"codex.config_overrides rejects {override}"
    return None
