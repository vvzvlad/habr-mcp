"""Tests for the shared startup helper `load_settings_or_exit`.

These use throwaway BaseSettings models defined inline (not the project's
`Settings`) so each test is hermetic and depends only on the env vars the test
sets/unsets via monkeypatch.
"""

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_errors import load_settings_or_exit


# Throwaway settings models. They explicitly do NOT read the project .env
# (env_file=None) so the tests depend only on the env vars they manage.
class _Req(BaseSettings):
    some_required_value: str
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


class _Ranged(BaseSettings):
    level: int = Field(ge=0, le=3)
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


# Has BOTH a required field (no default) AND a ranged field, so a single build
# can raise a "missing" error and an "invalid" error at the same time.
class _MissingAndRanged(BaseSettings):
    some_required_value: str
    level: int = Field(ge=0, le=3)
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


def test_missing_required_exits_with_clear_message(capsys, monkeypatch):
    monkeypatch.delenv("SOME_REQUIRED_VALUE", raising=False)
    with pytest.raises(SystemExit) as ei:
        load_settings_or_exit(_Req)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "SOME_REQUIRED_VALUE" in err
    assert "Missing required" in err


def test_invalid_value_exits_with_clear_message(capsys, monkeypatch):
    # Out-of-range value triggers a non-"missing" validation error.
    monkeypatch.setenv("LEVEL", "9")
    with pytest.raises(SystemExit) as ei:
        load_settings_or_exit(_Ranged)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "LEVEL" in err
    assert "Invalid" in err


def test_happy_path_returns_instance(monkeypatch):
    monkeypatch.setenv("SOME_REQUIRED_VALUE", "ok")
    obj = load_settings_or_exit(_Req)
    assert obj.some_required_value == "ok"


def test_multiple_errors_partition_into_correct_sections(capsys, monkeypatch):
    # One missing (unset) var and one invalid (out-of-range) var in one build,
    # so the helper must aggregate both and sort each into the right section.
    monkeypatch.delenv("SOME_REQUIRED_VALUE", raising=False)
    monkeypatch.setenv("LEVEL", "9")
    with pytest.raises(SystemExit) as ei:
        load_settings_or_exit(_MissingAndRanged)
    assert ei.value.code == 1
    err = capsys.readouterr().err

    # Both var names must appear in the report.
    assert "SOME_REQUIRED_VALUE" in err
    assert "LEVEL" in err

    # Locate the section headers so we can check which var lands under which.
    missing_hdr = err.index("Missing required variable(s):")
    invalid_hdr = err.index("Invalid value(s):")
    # Helper emits the missing section before the invalid one.
    assert missing_hdr < invalid_hdr

    req_pos = err.index("SOME_REQUIRED_VALUE")
    level_pos = err.index("LEVEL")

    # The required var must be listed in the Missing section (before Invalid),
    # and the ranged var in the Invalid section (after the Invalid header).
    assert missing_hdr < req_pos < invalid_hdr
    assert level_pos > invalid_hdr

    # Explicitly assert the partition is NOT inverted: the missing var is not
    # under Invalid, and the ranged var is not under Missing.
    assert not (req_pos > invalid_hdr)
    assert not (missing_hdr < level_pos < invalid_hdr)
