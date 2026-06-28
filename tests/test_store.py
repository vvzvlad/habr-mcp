"""Tests for the per-token credential store and its helpers."""

from __future__ import annotations

import json
import stat

import src.store as store_mod
from src.store import CredStore, _token_hash, derive_uuid_from_cookie, generate_key

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


TOKEN_A = "hmcp_token_a"
TOKEN_B = "hmcp_token_b"


def test_decode_foreign_key_returns_none(tmp_path):
    # Core security property: a record encrypted under TOKEN_A's key must NOT be
    # readable via TOKEN_B even if the on-disk store maps it under B's hash key
    # (rotated/foreign key). Fernet decrypt fails -> None, no creds leak.
    store = CredStore(str(tmp_path))
    store.set(TOKEN_A, COOKIE, "CSRF_A", "uuid-a")
    path = tmp_path / "creds.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    record_a = data[_token_hash(TOKEN_A)]
    # Re-key A's encrypted record under B's hash; the Fernet key still needs A.
    path.write_text(json.dumps({_token_hash(TOKEN_B): record_a}), encoding="utf-8")

    fresh = CredStore(str(tmp_path))
    assert fresh.get(TOKEN_B) is None  # decrypt under wrong token -> None


def test_decode_truncated_blob_returns_none(tmp_path):
    # A corrupted (sliced) ciphertext must degrade to None, not raise.
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    path = tmp_path / "creds.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    key = _token_hash(TOKEN)
    data[key]["blob"] = data[key]["blob"][:10]  # truncate ciphertext
    path.write_text(json.dumps(data), encoding="utf-8")

    fresh = CredStore(str(tmp_path))
    assert fresh.get(TOKEN) is None


def test_decode_missing_salt_returns_none(tmp_path):
    # A record missing its salt key must degrade to None (KeyError caught).
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    path = tmp_path / "creds.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    key = _token_hash(TOKEN)
    del data[key]["salt"]
    path.write_text(json.dumps(data), encoding="utf-8")

    fresh = CredStore(str(tmp_path))
    assert fresh.get(TOKEN) is None


def test_forced_hash_collision_still_isolated(tmp_path, monkeypatch):
    # Second defense layer: even if two tokens collide to the SAME record key,
    # the token-derived Fernet key differs, so TOKEN_B cannot read TOKEN_A's creds.
    monkeypatch.setattr(store_mod, "_token_hash", lambda token: "collision")
    store = CredStore(str(tmp_path))
    store.set(TOKEN_A, COOKIE, "CSRF_A", "uuid-a")
    # Same record key, different token -> Fernet decrypt fails -> None.
    assert store.get(TOKEN_B) is None
    # The legitimate owner still reads its own creds.
    assert store.get(TOKEN_A) == {"cookie": COOKIE, "csrf": "CSRF_A", "uuid": "uuid-a"}


def test_has_empty_token_is_false(tmp_path):
    # Empty token short-circuits before any lookup (no empty-identity match).
    store = CredStore(str(tmp_path))
    assert store.has("") is False


def test_derive_uuid_empty_value_returns_empty_string():
    # An explicit empty value matches the name and yields "" (NOT None).
    assert derive_uuid_from_cookie("foo=bar; habr_uuid=; baz=qux") == ""


def test_derive_uuid_name_superstring_does_not_match():
    # A name superstring like `xhabr_uuid` must not be treated as `habr_uuid`.
    assert derive_uuid_from_cookie("xhabr_uuid=abc") is None


def test_set_get_uuid_none_round_trips_as_none(tmp_path):
    # uuid=None must round-trip as None, never the literal string "None".
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF123", None)
    assert store.get(TOKEN) == {"cookie": COOKIE, "csrf": "CSRF123", "uuid": None}


def test_plaintext_fallback_round_trip(tmp_path, monkeypatch):
    # Plaintext fallback when cryptography is unavailable. Order-independent
    # thanks to resetting the process-global _WARN_STATE latch around the test.
    monkeypatch.setattr(store_mod, "_CRYPTO_AVAILABLE", False)
    saved = store_mod._WARN_STATE["plaintext"]
    store_mod._WARN_STATE["plaintext"] = False
    try:
        store = CredStore(str(tmp_path))
        store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
        data = json.loads((tmp_path / "creds.json").read_text(encoding="utf-8"))
        record = data[_token_hash(TOKEN)]
        assert record["enc"] is False  # stored as plaintext
        assert store.get(TOKEN) == {
            "cookie": COOKIE,
            "csrf": "CSRF123",
            "uuid": "uuid-x",
        }
    finally:
        store_mod._WARN_STATE["plaintext"] = saved


def test_load_corrupt_top_level_json_is_empty_store(tmp_path):
    # A corrupt top-level JSON file must load as an EMPTY store, not crash.
    (tmp_path / "creds.json").write_text("not json at all", encoding="utf-8")
    store = CredStore(str(tmp_path))
    assert store.get(TOKEN) is None
    assert store.has(TOKEN) is False


def test_load_non_dict_top_level_json_is_empty_store(tmp_path):
    # Valid JSON but a non-dict top level (a list) -> empty store.
    (tmp_path / "creds.json").write_text("[1,2,3]", encoding="utf-8")
    store = CredStore(str(tmp_path))
    assert store.get(TOKEN) is None
    assert store.has(TOKEN) is False


def test_persist_is_atomic_and_overwrites(tmp_path):
    # _persist writes via temp+replace: no leftover .tmp, and a later set wins.
    store = CredStore(str(tmp_path))
    store.set(TOKEN, COOKIE, "CSRF1", "uuid-1")
    assert not (tmp_path / "creds.json.tmp").exists()  # no temp residue
    store.set(TOKEN, "cookie2", "CSRF2", "uuid-2")  # overwrite same token
    assert not (tmp_path / "creds.json.tmp").exists()
    assert store.get(TOKEN) == {"cookie": "cookie2", "csrf": "CSRF2", "uuid": "uuid-2"}


def test_load_missing_file_does_not_create_it(tmp_path):
    # Construction on an empty dir must NOT create creds.json prematurely;
    # the file appears only on the first set().
    store = CredStore(str(tmp_path))
    assert not (tmp_path / "creds.json").exists()
    store.set(TOKEN, COOKIE, "CSRF123", "uuid-x")
    assert (tmp_path / "creds.json").exists()
