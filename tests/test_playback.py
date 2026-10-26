import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from orb.config import AppConfig
from orb.playback import PlaybackController, PlaybackError, resolve_player


def test_stop_interrupts_player(tmp_path: Path):
    script = tmp_path / "sleep.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    playback = PlaybackController(AppConfig(), command=[str(script)])
    thread = threading.Thread(target=lambda: playback.play(str(script)))
    thread.start()
    deadline = time.time() + 2
    while time.time() < deadline and not playback.is_playing():
        time.sleep(0.02)
    playback.stop()
    thread.join(2)
    assert not thread.is_alive()


def test_empty_path_does_not_start_a_player():
    playback = PlaybackController(AppConfig(), command=["/bin/true"])
    playback.play("")
    assert playback.is_playing() is False


def test_missing_player():
    if shutil.which("afplay") is None and sys.platform != "darwin":
        with pytest.raises(PlaybackError):
            resolve_player("afplay")
    else:
        assert resolve_player("afplay")[0] == "afplay"
