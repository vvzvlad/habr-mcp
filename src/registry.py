"""Per-user (per-token) ``HabrClient`` registry.

The server is multi-tenant: each bearer token maps to its own Habr credentials
and therefore its own ``HabrClient`` (its own httpx session, cookie and csrf).
This registry builds and caches one client per token, deriving a per-user
``Settings`` from a shared base ``Settings`` plus the token's stored creds.
"""

from __future__ import annotations

import asyncio
import hashlib

from src.client import HabrClient
from src.settings import Settings
from src.store import CredStore


def _token_hash(token: str) -> str:
    """Cache key: hash the token so the raw value is never used as a dict key."""
    return hashlib.sha256(token.encode()).hexdigest()


class ClientRegistry:
    """Builds and caches a ``HabrClient`` per bearer token.

    The base ``Settings`` carries the shared, non-secret config (language,
    proxy, timeout, user agent, page size). Per-user secrets
    (cookie/csrf/uuid) are layered on top from the ``CredStore``.
    """

    def __init__(self, base_settings: Settings, store: CredStore) -> None:
        self._base = base_settings
        self._store = store
        self._clients: dict[str, HabrClient] = {}
        self._lock = asyncio.Lock()

    def _user_settings(self, creds: dict[str, str]) -> Settings:
        """Clone the base settings and inject this user's Habr credentials."""
        return self._base.model_copy(
            update={
                "habr_cookie": creds.get("cookie"),
                "habr_csrf_token": creds.get("csrf"),
                "habr_user_uuid": creds.get("uuid"),
                # Author/write endpoints also accept the csrf via the Cookie's
                # double-submit pair; the full Cookie header already carries it.
            }
        )

    async def get(self, token: str | None) -> HabrClient | None:
        """Return a cached ``HabrClient`` for the token, or None if no creds.

        None means the token has no stored Habr credentials yet (NEEDS_LOGIN).

        The hot path never decrypts: a cache hit returns immediately, and a
        miss first does the cheap ``sha256``-only existence check before paying
        for PBKDF2 + Fernet, which is then run off the event loop via
        ``asyncio.to_thread`` so it never blocks other requests.
        """
        if not token:
            return None
        key = _token_hash(token)

        # Fast path: cache hit, no decryption (sha256 only).
        async with self._lock:
            client = self._clients.get(key)
        if client is not None:
            return client

        # Existence check is cheap (sha256 only); a miss avoids PBKDF2 entirely.
        if not self._store.has(token):
            return None

        # Expensive decryption (PBKDF2 + Fernet) off the event loop.
        creds = await asyncio.to_thread(self._store.get, token)
        if not creds:
            return None

        new_client = HabrClient(self._user_settings(creds))
        async with self._lock:
            # Double-checked: another coroutine may have built it meanwhile.
            existing = self._clients.get(key)
            if existing is not None:
                await new_client.aclose()
                return existing
            self._clients[key] = new_client
            return new_client

    async def invalidate(self, token: str | None) -> None:
        """Drop and close the cached client for a token, if any.

        Called after ``store.set`` on re-login so the NEXT ``get`` rebuilds the
        client from the fresh credentials instead of reusing a stale session
        (e.g. an expired Cookie). No-op when the token is None or not cached.
        """
        if not token:
            return
        key = _token_hash(token)
        async with self._lock:
            client = self._clients.pop(key, None)
        if client is not None:
            await client.aclose()

    async def aclose_all(self) -> None:
        """Close every cached client (called on server shutdown)."""
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()
