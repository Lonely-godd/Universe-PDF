import pytest

from universe_pdf.sessions import SessionError, SessionStore


def test_users_are_isolated_and_names_are_safe(tmp_path):
    store = SessionStore(tmp_path)
    one = store.get((1, 10))
    two = store.get((1, 20))
    path = store.allocate(one, "../../secret.png", 5)
    path.write_bytes(b"12345")
    store.accept(one, path)
    assert path.parent == one.directory
    assert len(one.files) == 1
    assert two.files == []
    store.reset((1, 10))
    assert not path.exists()
    assert two.directory.exists()


def test_limits_and_unsupported_format(tmp_path):
    store = SessionStore(tmp_path, max_files=1, max_file_bytes=5, max_total_bytes=5)
    session = store.get((1, 1))
    with pytest.raises(SessionError, match="формат"):
        store.allocate(session, "file.exe", 1)
    with pytest.raises(SessionError, match="размер"):
        store.allocate(session, "file.png", 6)
    path = store.allocate(session, "a.png", 5)
    path.write_bytes(b"12345")
    store.accept(session, path)
    with pytest.raises(SessionError):
        store.allocate(session, "b.png", 1)


def test_actual_size_is_checked(tmp_path):
    store = SessionStore(tmp_path, max_file_bytes=5)
    session = store.get((1, 1))
    path = store.allocate(session, "a.png", 1)
    path.write_bytes(b"123456")
    with pytest.raises(SessionError):
        store.accept(session, path)
    assert session.files == []
    assert not path.exists()


def test_expiry_preserves_busy_session(tmp_path):
    store = SessionStore(tmp_path, ttl_seconds=10)
    old = store.get((1, 1))
    busy = store.get((2, 2))
    old.touched = busy.touched = 0
    busy.busy = True
    store.expire(now=11)
    assert (1, 1) not in store.sessions
    assert store.sessions[(2, 2)] is busy


def test_capacity(tmp_path):
    store = SessionStore(tmp_path, max_sessions=1)
    store.get((1, 1))
    with pytest.raises(SessionError, match="очеред"):
        store.get((2, 2))
