import os

import pytest

from sgfl import util
from sgfl.util import SGFLError


@pytest.mark.parametrize(
    "value,expected",
    [
        ("short", "*****"),
        ("12345678", "********"),
        ("abcdefghij", "abcd...ghij"),
    ],
)
def test_mask_secret(value, expected):
    assert util.maskSecret(value) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2.1.0", (2, 1, 0)),
        ("2.10.3", (2, 10, 3)),
        ("2.1.0rc1", (2, 1, 0)),
        ("v2", ()),  # non-numeric leading chunk stops parsing immediately
        ("", ()),
    ],
)
def test_parse_version_tuple(raw, expected):
    assert util._parseVersionTuple(raw) == expected


@pytest.mark.parametrize(
    "remote,local,expected",
    [
        ("2.2.0", "2.1.0", True),
        ("2.1.0", "2.1.0", False),
        ("2.0.9", "2.1.0", False),
        ("garbage", "2.1.0", False),
    ],
)
def test_is_newer_version(remote, local, expected):
    assert util._isNewerVersion(remote, local) is expected


def test_clip_output_short_text_returned_stripped():
    assert util._clipOutput("  hello  ") == "hello"


def test_clip_output_none_returns_none():
    assert util._clipOutput(None) is None


def test_clip_output_blank_returns_none():
    assert util._clipOutput("   ") is None


def test_clip_output_truncates_long_text():
    text = "x" * 3000
    clipped = util._clipOutput(text, maxChars=10)
    assert clipped.startswith("x" * 10)
    assert clipped.endswith("...[output truncated]")


def test_format_command_quotes_args_with_spaces():
    assert util._formatCommand(["rojo", "build", "my project.json"]) == "rojo build 'my project.json'"


def test_get_env_safe_missing_raises(monkeypatch):
    monkeypatch.delenv("SOME_UNSET_KEY", raising=False)
    with pytest.raises(SGFLError, match="SOME_UNSET_KEY"):
        util.getEnvSafe("SOME_UNSET_KEY")


def test_get_env_safe_returns_value(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "value123")
    assert util.getEnvSafe("SOME_KEY") == "value123"


def test_get_env_safe_credential_key_suggests_auth_login(monkeypatch):
    monkeypatch.delenv("PUBLISH_KEY", raising=False)
    with pytest.raises(SGFLError) as excinfo:
        util.getEnvSafe("PUBLISH_KEY")
    assert any("auth login" in s for s in excinfo.value.suggestions)


def test_discover_place_ids_bare_place_id_becomes_main(monkeypatch):
    monkeypatch.delenv("PLACE_ID_MAIN", raising=False)
    for key in list(os.environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLACE_ID", "12345")
    assert util.discoverPlaceIds() == {"main": "12345"}


def test_discover_place_ids_named_entries(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLACE_ID_LOBBY", "111")
    monkeypatch.setenv("PLACE_ID_ARENA", "222")
    assert util.discoverPlaceIds() == {"lobby": "111", "arena": "222"}


def test_discover_place_ids_conflicting_bare_and_main_rejected(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLACE_ID", "111")
    monkeypatch.setenv("PLACE_ID_MAIN", "222")
    with pytest.raises(SGFLError, match="same place name"):
        util.discoverPlaceIds()


def test_discover_place_ids_non_numeric_rejected(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLACE_ID_LOBBY", "not-a-number")
    with pytest.raises(SGFLError, match="invalid"):
        util.discoverPlaceIds()


def test_discover_place_ids_none_set_rejected(monkeypatch):
    for key in list(os.environ):
        if key.startswith("PLACE_ID"):
            monkeypatch.delenv(key, raising=False)
    with pytest.raises(SGFLError, match="No place ID"):
        util.discoverPlaceIds()


def test_confirm_publish_refuses_non_tty(monkeypatch):
    monkeypatch.setattr(util.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SGFLError, match="non-interactive"):
        util.confirmPublish("prod", ["some summary line"])
