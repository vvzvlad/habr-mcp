"""Tests for the per-token credential store and its helpers."""

from __future__ import annotations

import json
import stat

from src.store import CredStore, derive_uuid_from_cookie, generate_key

COOKIE = "connect_sid=s%3Aabc; habr_uuid=11111111-2222-3333; hsec_id=def"
TOKEN = "hmcp_roundtrip_token"


def test_set_get_round_trip(tmp_path):
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    creds = store.get(TOKEN)
    assert creds == {"cookie": COOKIE, "csrf": "CSRF123", "uuid": "uuid-x"}


def test_get_unknown_token_returns_none(tmp_path):
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    assert store.get("hmcp_some_other_token") is None
    assert store.get("") is None


def test_has_reflects_presence(tmp_path):
    store = CredStore(str(tmp_path))
    assert store.has(TOKEN) is False
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    assert store.has(TOKEN) is True


def test_persisted_across_instances(tmp_path):
    CredStore(str(tmp_path)).set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    # A fresh instance must load the same record from disk.
    assert CredStore(str(tmp_path)).get(TOKEN) == {
        "cookie": COOKIE,
        "csrf": "CSRF123",
        "uuid": "uuid-x",
    }


def test_on_disk_file_does_not_leak_plaintext_cookie(tmp_path):
    # With cryptography available the encrypted blob must not contain the cookie.
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    raw = (tmp_path / "creds.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    record = next(iter(data.values()))
    assert record["enc"] is True
    assert COOKIE not in raw
    assert "CSRF123" not in raw


def test_file_mode_is_0600(tmp_path):
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    mode = stat.S_IMODE((tmp_path / "creds.json").stat().st_mode)
    assert mode == 0o600


def test_state_dir_mode_is_0700(tmp_path):
    state_dir = tmp_path / "state"
    store = CredStore(str(state_dir))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    mode = stat.S_IMODE(state_dir.stat().st_mode)
    assert mode == 0o700


def test_corrupt_non_dict_record_returns_none(tmp_path):
    # A tampered/partial store may map a key to a non-dict value (here a string).
    # get() must degrade to None (-> NEEDS_LOGIN), never raise AttributeError.
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    path = tmp_path / "creds.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    key = next(iter(data))
    data[key] = "totally-not-a-record"
    path.write_text(json.dumps(data), encoding="utf-8")

    fresh = CredStore(str(tmp_path))
    assert fresh.get(TOKEN) is None  # no exception


def test_derive_uuid_from_cookie_decodes_url_encoding():
    cookie = "foo=bar; habr_uuid=abc%2Fdef; baz=qux"
    assert derive_uuid_from_cookie(cookie) == "abc/def"


def test_derive_uuid_from_cookie_missing_returns_none():
    assert derive_uuid_from_cookie("foo=bar; baz=qux") is None


def test_generate_key_is_prefixed_and_unique():
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50  # all unique
    assert all(k.startswith("hmcp_") for k in keys)
