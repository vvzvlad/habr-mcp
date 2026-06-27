"""Tests for the per-token ``ClientRegistry``: caching, re-login invalidation,
and the cheap-cache-first / offloaded-decryption hot path.
"""

from __future__ import annotations

import src.registry as registry_mod
from src.registry import ClientRegistry
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
