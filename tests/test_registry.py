"""Tests for the per-token ``ClientRegistry``: caching, re-login invalidation,
and the cheap-cache-first / offloaded-decryption hot path.
"""

from __future__ import annotations

import asyncio

import src.client as client_mod
import src.registry as registry_mod
import src.store as store_mod
from src.registry import ClientRegistry, _token_hash
from src.settings import Settings
from src.store import CredStore

COOKIE_OLD = "connect_sid=s%3Aold; habr_uuid=uuid-old; hsec_id=x"
COOKIE_NEW = "connect_sid=s%3Anew; habr_uuid=uuid-new; hsec_id=y"
TOKEN = "hmcp_reg_token"


def _make_registry(tmp_path):
    store = CredStore(str(tmp_path))
    registry = ClientRegistry(Settings(state_dir=str(tmp_path)), store)
    return store, registry


async def test_get_returns_none_without_creds(tmp_path):
    _store, registry = _make_registry(tmp_path)
    assert await registry.get(None) is None
    assert await registry.get("hmcp_unknown") is None


async def test_get_caches_same_client(tmp_path):
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")
    first = await registry.get(TOKEN)
    second = await registry.get(TOKEN)
    assert first is not None
    # A second get returns the very same cached instance (no rebuild).
    assert first is second
    await registry.aclose_all()


async def test_relogin_rebuilds_client_with_new_cookie(tmp_path):
    """After set -> get -> set(new cookie) -> invalidate, the next get builds a
    NEW client carrying the new cookie, and the old client was closed.
    """
    store, registry = _make_registry(tmp_path)

    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")
    old_client = await registry.get(TOKEN)
    assert old_client is not None
    assert old_client._settings.habr_cookie == COOKIE_OLD

    # Re-login with a fresh Cookie (e.g. the Habr session expired).
    store.set(TOKEN, COOKIE_NEW, "CSRF2", "uuid-new")
    await registry.invalidate(TOKEN)

    # The stale client was closed by invalidate.
    assert old_client._client.is_closed

    new_client = await registry.get(TOKEN)
    assert new_client is not None
    # A genuinely new instance carrying the new cookie/csrf.
    assert new_client is not old_client
    assert new_client._settings.habr_cookie == COOKIE_NEW
    assert new_client._settings.habr_csrf_token == "CSRF2"
    await registry.aclose_all()


async def test_invalidate_noop_when_not_cached(tmp_path):
    _store, registry = _make_registry(tmp_path)
    # No exception when the token is None or was never cached.
    await registry.invalidate(None)
    await registry.invalidate("hmcp_never_seen")


async def test_get_cache_hit_does_not_decrypt(tmp_path, monkeypatch):
    """A cache hit must not touch the store (no PBKDF2/Fernet on the hot path)."""
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")
    first = await registry.get(TOKEN)  # builds + caches (one decrypt)
    assert first is not None

    calls = {"get": 0, "has": 0}
    real_get = store.get
    real_has = store.has

    def counting_get(token):
        calls["get"] += 1
        return real_get(token)

    def counting_has(token):
        calls["has"] += 1
        return real_has(token)

    monkeypatch.setattr(store, "get", counting_get)
    monkeypatch.setattr(store, "has", counting_has)

    second = await registry.get(TOKEN)
    assert second is first
    # Cache hit: neither the cheap existence check nor the expensive decrypt ran.
    assert calls["get"] == 0
    assert calls["has"] == 0
    await registry.aclose_all()


async def test_get_miss_uses_has_then_offloads_decrypt(tmp_path, monkeypatch):
    """A cache miss does the cheap ``has`` check and offloads ``store.get`` to a
    worker thread via ``asyncio.to_thread``.
    """
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")

    offloaded = {"count": 0}
    real_to_thread = registry_mod.asyncio.to_thread

    async def spy_to_thread(func, *args, **kwargs):
        offloaded["count"] += 1
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(registry_mod.asyncio, "to_thread", spy_to_thread)

    client = await registry.get(TOKEN)
    assert client is not None
    # The expensive decryption was offloaded off the event loop exactly once.
    assert offloaded["count"] == 1
    await registry.aclose_all()


async def test_get_none_when_store_get_returns_none(tmp_path, monkeypatch):
    """has()=True but store.get()->None (tampered/foreign record): get returns
    None, builds no client, caches nothing. Restoring real get rebuilds later.
    """
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")

    monkeypatch.setattr(store, "get", lambda token: None)  # simulate decrypt fail
    assert await registry.get(TOKEN) is None
    # No client was built or cached for this token.
    assert _token_hash(TOKEN) not in registry._clients

    monkeypatch.undo()  # restore the real decrypting get
    client = await registry.get(TOKEN)
    assert client is not None
    assert _token_hash(TOKEN) in registry._clients
    await registry.aclose_all()


async def test_cross_tenant_isolation(tmp_path):
    # Different tokens must yield distinct clients, each carrying its own cookie.
    store, registry = _make_registry(tmp_path)
    store.set("hmcp_tenant_a", COOKIE_OLD, "CSRF_A", "uuid-old")
    store.set("hmcp_tenant_b", COOKIE_NEW, "CSRF_B", "uuid-new")

    client_a = await registry.get("hmcp_tenant_a")
    client_b = await registry.get("hmcp_tenant_b")
    assert client_a is not None and client_b is not None
    assert client_a is not client_b  # separate instances
    assert client_a._settings.habr_cookie == COOKIE_OLD
    assert client_b._settings.habr_cookie == COOKIE_NEW
    await registry.aclose_all()


async def test_user_settings_injects_creds_without_mutating_base(tmp_path):
    # _user_settings layers creds onto a COPY; the base Settings stays untouched.
    _store, registry = _make_registry(tmp_path)
    assert registry._base.habr_cookie is None  # precondition
    settings = registry._user_settings(
        {"cookie": "C", "csrf": "S", "uuid": "U"}
    )
    assert settings.habr_cookie == "C"
    assert settings.habr_csrf_token == "S"
    assert settings.habr_user_uuid == "U"
    # The shared base must not be mutated by the per-user copy.
    assert registry._base.habr_cookie is None


def test_token_hash_matches_store(tmp_path):
    # Cross-module invariant: the registry cache key equals the store record key.
    token = "hmcp_invariant_token"
    assert _token_hash(token) == store_mod._token_hash(token)


async def test_aclose_all_closes_and_clears(tmp_path):
    # aclose_all closes underlying httpx clients and empties the cache; a later
    # get builds a fresh instance.
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")
    client1 = await registry.get(TOKEN)
    assert client1 is not None

    await registry.aclose_all()
    assert client1._client.is_closed  # underlying httpx client closed
    assert registry._clients == {}  # cache emptied

    client2 = await registry.get(TOKEN)
    assert client2 is not None
    assert client2 is not client1  # rebuilt anew
    await registry.aclose_all()


async def test_double_checked_lock_caches_one_and_closes_loser(
    tmp_path, monkeypatch
):
    """Concurrent misses both pass has() and build a client (asyncio.to_thread
    yields), but the lock must cache exactly one; the loser is closed.
    """
    store, registry = _make_registry(tmp_path)
    store.set(TOKEN, COOKIE_OLD, "CSRF1", "uuid-old")

    counts = {"built": 0, "closed": 0}
    real_init = client_mod.HabrClient.__init__
    real_aclose = client_mod.HabrClient.aclose

    def counting_init(self, settings):
        counts["built"] += 1
        real_init(self, settings)

    async def counting_aclose(self):
        counts["closed"] += 1
        await real_aclose(self)

    monkeypatch.setattr(client_mod.HabrClient, "__init__", counting_init)
    monkeypatch.setattr(client_mod.HabrClient, "aclose", counting_aclose)

    a, b = await asyncio.gather(registry.get(TOKEN), registry.get(TOKEN))
    assert a is b  # exactly one client wins
    assert len(registry._clients) == 1  # only one cached
    # The race genuinely fired: both coroutines built a client (asyncio.to_thread
    # always yields control), and the lock kept exactly one — the loser was closed.
    assert counts["built"] == 2
    assert counts["closed"] == 1

    await registry.aclose_all()
