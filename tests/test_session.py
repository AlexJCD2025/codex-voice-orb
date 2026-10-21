import json
import os

from orb.session import SessionStore


def test_session_round_trip(tmp_path):
    store = SessionStore(tmp_path / "session.json")
    assert store.load() is None
    store.save("thread-123", "/work/repo")
    assert (store.path.stat().st_mode & 0o777) == 0o600
    state = store.load()
    assert state is not None
    assert state.thread_id == "thread-123"
    assert state.cwd == "/work/repo"
    assert state.updated_at
    assert store.clear() is True
    assert store.load() is None
    assert store.clear() is False


def test_corrupt_session_is_ignored(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{", encoding="utf-8")
    assert SessionStore(path).load() is None
    path.write_text(json.dumps({"thread_id": ""}), encoding="utf-8")
    assert SessionStore(path).load() is None
    os.chmod(path, 0o600)
