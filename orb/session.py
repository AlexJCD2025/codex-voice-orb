"""Persist the Codex thread id used by `codex exec resume`."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SessionState:
    thread_id: str
    cwd: str | None
    updated_at: str


def ensure_private_dir(path: Path) -> None:
    """Create a directory and tighten it when it is new or belongs to the orb."""
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if created or ".codex-voice-orb" in path.parts:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass


class SessionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def load(self) -> SessionState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        thread_id = data.get("thread_id")
        if not thread_id or not isinstance(thread_id, str):
            return None
        cwd = data.get("cwd")
        updated = data.get("updated_at") or ""
        return SessionState(thread_id=thread_id, cwd=cwd if isinstance(cwd, str) else None, updated_at=str(updated))

    def save(self, thread_id: str, cwd: str | None) -> None:
        ensure_private_dir(self.path.parent)
        payload = {
            "thread_id": thread_id,
            "cwd": cwd,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        self.path.unlink()
        return True
