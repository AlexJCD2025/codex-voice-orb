"""Backend interface used by the orchestrator."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol, Sequence


class BackendError(RuntimeError):
    """Base error for agent backends."""


class CodexCancelled(BackendError):
    """The in-flight Codex process was interrupted."""


class CodexTimeout(BackendError):
    """Codex exceeded the configured turn timeout."""


class HighRiskActionBlocked(BackendError):
    """A tool or shell action was stopped because this turn was not confirmed."""

    def __init__(self, action: str):
        super().__init__(action)
        self.action = action


class CodexExecError(BackendError):
    def __init__(self, message: str, *, text: str = "", session_id: str | None = None):
        super().__init__(message)
        self.text = text
        self.session_id = session_id


class CancelToken:
    def __init__(self) -> None:
        self._flag = threading.Event()

    def cancel(self) -> None:
        self._flag.set()

    def is_set(self) -> bool:
        return self._flag.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._flag.wait(timeout)


@dataclass(frozen=True)
class BackendResult:
    text: str
    session_id: str | None = None
    full_text: str = ""
    commands: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.full_text:
            object.__setattr__(self, "full_text", self.text)


class Backend(Protocol):
    def plan(
        self,
        prompt: str,
        *,
        images: Sequence[str] = (),
        cancel: CancelToken | None = None,
    ) -> object: ...

    def execute(
        self,
        prompt: str,
        actions: Sequence[object],
        *,
        images: Sequence[str] = (),
        resume_id: str | None = None,
        cancel: CancelToken | None = None,
    ) -> BackendResult: ...

    def always_requires_confirmation(self) -> bool: ...
