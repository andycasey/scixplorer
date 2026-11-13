from __future__ import annotations

from pathlib import Path

import pytest

from ads._config import TOKEN_ENV_VARS, TOKEN_FILES, Config, config


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """An empty home directory with no token environment variables set."""
    for name in TOKEN_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def write_token(home: Path, name: str, text: str, directory: str = ".ads") -> None:
    (home / directory).mkdir(exist_ok=True)
    (home / directory / name).write_text(text)


def test_defaults():
    c = Config()
    assert c.token is None
    assert c.api_url == "https://api.scixplorer.org/v1"
    assert c.timeout == 30.0
    assert c.max_retries == 3
    assert c.retry_backoff == 1.0
    assert c.page_size == 200
    assert c.max_inline_bibcodes == 50
    assert c.library_membership == "docs"
    assert c.collection is None


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


def test_token_locations():
    assert TOKEN_ENV_VARS == ("SCIX_API_TOKEN", "ADS_API_TOKEN", "ADS_DEV_KEY")
    assert TOKEN_FILES == (".scix/token", ".ads/token", ".ads/dev_key")


def test_scix_env_var_comes_first(home, monkeypatch):
    monkeypatch.setenv("ADS_DEV_KEY", "dev-key")
    monkeypatch.setenv("ADS_API_TOKEN", "api-token")
    monkeypatch.setenv("SCIX_API_TOKEN", " scix-token ")
    assert Config().resolve_token() == "scix-token"
    monkeypatch.setenv("SCIX_API_TOKEN", "  ")
    assert Config().resolve_token() == "api-token"


def test_scix_token_file_comes_first(home):
    write_token(home, "dev_key", "from-dev-key")
    write_token(home, "token", "from-ads-token")
    write_token(home, "token", " from-scix-token \n", directory=".scix")
    assert Config().resolve_token() == "from-scix-token"
    write_token(home, "token", "\n", directory=".scix")
    assert Config().resolve_token() == "from-ads-token"


def test_ads_env_var_beats_scix_file(home, monkeypatch):
    write_token(home, "token", "from-scix-file", directory=".scix")
    monkeypatch.setenv("ADS_DEV_KEY", "env")
    assert Config().resolve_token() == "env"


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


@pytest.mark.parametrize(
    "token",
    [
        [" a ", "b", "", "a", "  ", "c\n"],
        (" a ", "b", "", "a", "  ", "c\n"),
        (t for t in [" a ", "b", "", "a", "  ", "c\n"]),
    ],
    ids=["list", "tuple", "generator"],
)
def test_several_tokens_stripped_deduplicated_blanks_dropped(home, token):
    c = Config(token=token)
    assert c.resolve_tokens() == ("a", "b", "c")
    assert c.resolve_tokens() == ("a", "b", "c")
    assert c.resolve_token() == "a"


def test_generator_assigned_later_is_consumed_once(home):
    c = Config()
    c.token = (t for t in ["x", "y"])
    assert c.token == ("x", "y")
    assert c.resolve_tokens() == ("x", "y")
    assert c.resolve_tokens() == ("x", "y")


def test_single_token_resolves_to_one_tuple(home):
    assert Config(token=" only ").resolve_tokens() == ("only",)


@pytest.mark.parametrize(
    ("token", "type_name"),
    [
        (b"s3cret", "bytes"),
        (["qwerty", b"s3cret"], "bytes"),
        (["qwerty", 12345], "int"),
        (12345, "int"),
    ],
)
def test_bad_token_types_rejected_without_echoing_value(home, token, type_name):
    c = Config(token=token)
    with pytest.raises(TypeError) as info:
        c.resolve_tokens()
    message = str(info.value)
    assert type_name in message
    assert "s3cret" not in message and "12345" not in message and "qwerty" not in message


def test_blank_token_list_falls_through_to_env(home, monkeypatch):
    monkeypatch.setenv("ADS_DEV_KEY", " env ")
    assert Config(token=["", " "]).resolve_tokens() == ("env",)


def test_resolve_tokens_precedence(home, monkeypatch):
    write_token(home, "token", "file-1")
    write_token(home, "dev_key", "file-2")
    assert Config().resolve_tokens() == ("file-1",)
    monkeypatch.setenv("ADS_DEV_KEY", "env-2")
    assert Config().resolve_tokens() == ("env-2",)
    monkeypatch.setenv("ADS_API_TOKEN", "env-1")
    assert Config().resolve_tokens() == ("env-1",)
    assert Config(token=["t1", "t2"]).resolve_tokens() == ("t1", "t2")


def test_env_value_not_split(home, monkeypatch):
    monkeypatch.setenv("ADS_API_TOKEN", "a,b")
    assert Config().resolve_tokens() == ("a,b",)


def test_no_tokens_anywhere(home):
    assert Config().resolve_tokens() == ()


def test_several_tokens_not_in_repr():
    assert "s3cret" not in repr(Config(token=["s3cret-1", "s3cret-2"]))


@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        (None, ()),
        ("", ()),
        (" astronomy ", ("astronomy",)),
        ([], ()),
        (["  ", ""], ()),
        (["physics", " astronomy", "", "physics", "astronomy "], ("physics", "astronomy")),
        (("earthscience",), ("earthscience",)),
    ],
)
def test_resolve_collections(collection, expected):
    assert Config(collection=collection).resolve_collections() == expected


@pytest.mark.parametrize(
    ("collection", "match"),
    [
        (["astronomy", 3], "collection 2 must be a str, not int"),
        ([None], "collection 1 must be a str, not NoneType"),
        (b"astronomy", "must be a str or a sequence of str, not bytes"),
        ({"astronomy"}, "must be a str or a sequence of str, not set"),
        (3, "must be a str or a sequence of str, not int"),
    ],
)
def test_bad_collection_settings(collection, match):
    with pytest.raises(TypeError, match=match):
        Config(collection=collection).resolve_collections()
