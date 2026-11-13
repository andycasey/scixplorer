from __future__ import annotations

from pathlib import Path

import pytest

from ads._config import TOKEN_ENV_VARS, Config, config


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty home directory with no token environment variables set."""
    for name in TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def write_token(home: Path, name: str, text: str) -> None:
    (home / ".ads").mkdir(exist_ok=True)
    (home / ".ads" / name).write_text(text)


def test_defaults():
    c = Config()
    assert c.token is None
    assert c.api_url == "https://api.adsabs.harvard.edu/v1"
    assert c.timeout == 30.0
    assert c.max_retries == 3
    assert c.retry_backoff == 1.0
    assert c.page_size == 200
    assert c.max_inline_bibcodes == 50
    assert c.library_membership == "docs"


def test_module_level_instance():
    assert isinstance(config, Config)


def test_no_token_anywhere(home):
    assert Config().resolve_token() is None


def test_explicit_token_wins(home, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "env")
    write_token(home, "token", "file")
    assert Config(token="  explicit \n").resolve_token() == "explicit"


def test_blank_explicit_token_falls_through(home, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "env")
    assert Config(token="   ").resolve_token() == "env"


def test_env_vars_in_order(home, monkeypatch):
    monkeypatch.setenv("ADS_DEV_KEY", "dev-key")
    assert Config().resolve_token() == "dev-key"
    monkeypatch.setenv("ADS_API_TOKEN", " api-token ")
    assert Config().resolve_token() == "api-token"


def test_empty_env_var_skipped(home, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "  ")
    monkeypatch.setenv("ADS_DEV_KEY", "dev-key")
    assert Config().resolve_token() == "dev-key"


def test_env_beats_files(home, monkeypatch):
    write_token(home, "token", "file")
    monkeypatch.setenv("ADS_DEV_KEY", "env")
    assert Config().resolve_token() == "env"


def test_files_in_order(home):
    write_token(home, "dev_key", "from-dev-key\n")
    assert Config().resolve_token() == "from-dev-key"
    write_token(home, "token", "  from-token  \nsecond line\n")
    assert Config().resolve_token() == "from-token"


def test_empty_token_file_skipped(home):
    write_token(home, "token", "\n")
    write_token(home, "dev_key", "from-dev-key")
    assert Config().resolve_token() == "from-dev-key"


def test_resolved_at_call_time(home, monkeypatch):
    c = Config()
    assert c.resolve_token() is None
    monkeypatch.setenv("ADS_API_TOKEN", "later")
    assert c.resolve_token() == "later"


def test_token_not_in_repr():
    assert "s3cret" not in repr(Config(token="s3cret"))
