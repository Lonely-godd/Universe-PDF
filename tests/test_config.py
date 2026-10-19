import pytest

from universe_pdf.config import Settings


def test_token_required(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValueError, match="BOT_TOKEN"):
        Settings.from_env(load_file=False)


def test_settings(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:example")
    monkeypatch.setenv("ALLOWED_USER_IDS", "123,456")
    settings = Settings.from_env(load_file=False)
    assert settings.allowed_user_ids == frozenset({123, 456})
    assert "123:example" not in repr(settings)


@pytest.mark.parametrize(
    "name,value",
    [("MAX_FILES", "0"), ("MAX_FILE_BYTES", "20000001"), ("ALLOWED_USER_IDS", "hello")],
)
def test_invalid_settings(monkeypatch, name, value):
    monkeypatch.setenv("BOT_TOKEN", "123:example")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env(load_file=False)
