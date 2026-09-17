"""Configuration loading, and why the API key goes unseen.

Every case here presents identically to a user -- the app quietly runs in demo
mode, or dies with an unreadable traceback -- so each one is worth a test that
names it.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from dotenv import dotenv_values


def write(tmp_path, content: bytes):
    path = tmp_path / ".env"
    path.write_bytes(content)
    return path


def test_plain_utf8_parses(tmp_path):
    assert dotenv_values(write(tmp_path, b"YOUTUBE_API_KEY=AIzaKEY\n"))[
        "YOUTUBE_API_KEY"
    ] == "AIzaKEY"


def test_a_utf8_bom_is_tolerated(tmp_path):
    # Notepad's "Save as UTF-8" can prepend one.
    path = write(tmp_path, b"\xef\xbb\xbfYOUTUBE_API_KEY=AIzaKEY\n")
    assert dotenv_values(path)["YOUTUBE_API_KEY"] == "AIzaKEY"


def test_crlf_is_tolerated(tmp_path):
    path = write(tmp_path, b"YOUTUBE_API_KEY=AIzaKEY\r\n")
    assert dotenv_values(path)["YOUTUBE_API_KEY"] == "AIzaKEY"


def test_quotes_and_spaces_are_tolerated(tmp_path):
    path = write(tmp_path, b'YOUTUBE_API_KEY = "AIzaKEY"\n')
    assert dotenv_values(path)["YOUTUBE_API_KEY"] == "AIzaKEY"


def test_utf16_cannot_be_parsed(tmp_path):
    # Windows PowerShell 5.1 writes this with '>' redirection.
    path = write(tmp_path, b"\xff\xfe" + "YOUTUBE_API_KEY=AIzaKEY\n".encode("utf-16-le"))
    with pytest.raises(UnicodeDecodeError):
        dotenv_values(path)


def test_a_trailing_blank_duplicate_wins(tmp_path):
    # Exactly what happens when .env.example's empty line is left below the
    # real key: the last occurrence wins and the key reads as empty.
    path = write(tmp_path, b"YOUTUBE_API_KEY=AIzaKEY\nYOUTUBE_API_KEY=\n")
    assert dotenv_values(path)["YOUTUBE_API_KEY"] == ""


# -- the diagnostics -------------------------------------------------------


def reload_config():
    import social_listener.config as config

    return importlib.reload(config)


def test_env_path_is_anchored_to_the_project_not_the_cwd(monkeypatch, tmp_path):
    """The original defect: a bare load_dotenv() resolves against the cwd, so
    the key was found only when you happened to run from the repo root."""
    import social_listener.config as config

    monkeypatch.chdir(tmp_path)
    reloaded = reload_config()
    assert reloaded.ENV_PATH == reloaded.PROJECT_ROOT / ".env"
    assert str(tmp_path) not in str(reloaded.ENV_PATH)


def test_diagnose_reports_a_missing_env(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    reloaded = reload_config()
    if reloaded.ENV_PATH.exists():
        pytest.skip("a real .env exists in the working tree")
    report = reloaded.diagnose_env()
    assert report["mode"] == "demo"
    assert any("No .env" in p for p in report["problems"])


def test_diagnose_never_prints_the_whole_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456")
    reloaded = reload_config()
    report = reloaded.diagnose_env()
    assert report["key_seen"] is True
    fingerprint = report["key_fingerprint"]
    assert "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456" not in fingerprint
    assert fingerprint.startswith("AIzaSy")


def test_diagnose_flags_a_suspiciously_short_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "tooshort")
    reloaded = reload_config()
    assert any("characters" in p for p in reloaded.diagnose_env()["problems"])


def test_mode_is_live_only_with_a_key(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456")
    assert reload_config().settings.mode == "live"
    monkeypatch.delenv("YOUTUBE_API_KEY")
    reloaded = reload_config()
    if not reloaded.ENV_PATH.exists():
        assert reloaded.settings.mode == "demo"


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    reload_config()
