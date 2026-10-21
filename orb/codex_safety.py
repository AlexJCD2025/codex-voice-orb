"""Guards that keep Codex invocations inside the voice-orb safety policy."""

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
DEFAULT_SANDBOXES = ("read-only", "workspace-write")

_FLAG_WITH_VALUE = {
    "-s",
    "--sandbox",
    "-m",
    "-c",
    "--config",
    "-C",
    "--cd",
    "-i",
    "--image",
    "-o",
    "--output-last-message",
    "-p",
    "--profile",
}


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
        index += 1
    return values


def override_sets_never(override: str) -> bool:
    compact = override.replace(" ", "").lower()
    return "approval_policy" in compact and "never" in compact


def override_sets_danger(override: str) -> bool:
    compact = override.replace(" ", "").lower()
    return "danger-full-access" in compact or "danger_full_access" in compact


def assert_argv_safe(
    argv: list[str],
    *,
    confirmed: bool,
    allow_full_auto: bool,
    allow_danger_full_access: bool,
) -> None:
    """Reject full-auto and unsandboxed runs unless this turn was confirmed and opted in."""
    problems: list[str] = []
    unlocked = bool(confirmed and allow_full_auto)
    danger_ok = bool(confirmed and allow_danger_full_access)
    for arg in argv:
        if arg in FORBIDDEN_FLAGS and not unlocked:
            problems.append(arg)
    for value in sandbox_values(argv):
        if value not in ALLOWED_SANDBOXES:
            problems.append(f"sandbox {value}")
        elif value == "danger-full-access" and not danger_ok:
            problems.append("danger-full-access")
    for override in config_override_values(argv):
        if override_sets_never(override) and not unlocked:
            problems.append(override)
        if override_sets_danger(override) and not danger_ok:
            problems.append(override)
    if problems:
        raise UnsafeCodexInvocation(
            "refusing unsafe codex invocation: " + ", ".join(problems)
        )


def unsafe_config_reason(
    *,
    extra_args: tuple[str, ...],
    config_overrides: tuple[str, ...],
    sandbox: str,
    allow_full_auto: bool,
    allow_danger_full_access: bool,
) -> str | None:
    """Return a config-time error string when defaults would enable full-auto."""
    if sandbox not in ALLOWED_SANDBOXES:
        return f"codex.sandbox must be one of {', '.join(ALLOWED_SANDBOXES)}"
    if sandbox == "danger-full-access" and not allow_danger_full_access:
        return "sandbox danger-full-access requires permissions.allow_danger_full_access: true"
    # confirmed=True here only checks that the file is allowed to *ask*.
    # CodexCliBackend still refuses the same flags until the spoken/key confirm.
    fake_argv = ["codex", "exec", "-s", sandbox, *extra_args]
    for override in config_overrides:
        fake_argv.extend(["-c", override])
    try:
        assert_argv_safe(
            fake_argv,
            confirmed=True,
            allow_full_auto=allow_full_auto,
            allow_danger_full_access=allow_danger_full_access,
        )
    except UnsafeCodexInvocation as exc:
        return str(exc)
    return None
