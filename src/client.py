"""Async HTTP client for Habr's undocumented internal JSON API.

Everything Habr-route-specific (URLs, query params, request bodies, auth headers)
is centralized here so the routes are easy to adjust if Habr changes them. Read
methods are anonymous; write methods require a logged-in session supplied via
settings (connect.sid cookie + CSRF token).
"""

from __future__ import annotations

import html as html_module
from typing import Any

import httpx

from src.settings import Settings

# Base for every endpoint; trailing slash matters for httpx relative URL joins.
BASE_URL = "https://habr.com/kek/v2/"

# Message shown when a write tool is called without credentials configured.
MISSING_CREDS_MESSAGE = (
    "Для записи нужны HABR_CONNECT_SID и HABR_CSRF_TOKEN "
    "(получите их из cookie залогиненного браузера)."
)


class HabrApiError(Exception):
    """Raised for any Habr API failure (HTTP error dict, bad body, transport)."""


def _wrap_html(text: str) -> str:
    """Habr expects HTML in comment bodies.

    If the caller's text already contains a tag, send it as-is; otherwise escape
    ``& < >`` and wrap it in a single paragraph.
    """
    if "<" in text:
        return text
    return "<p>" + html_module.escape(text, quote=False) + "</p>"


class HabrClient:
    """Thin async wrapper over the Habr ``kek/v2`` API."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        lang = settings.habr_lang
        # `fl` = content/flow language, `hl` = interface language; sent on every GET.
        self._default_params: dict[str, str] = {"fl": lang, "hl": lang}
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=settings.request_timeout,
            headers={"User-Agent": settings.user_agent},
            proxy=settings.proxy or None,
        )

    # -- low-level helpers --------------------------------------------------

    @staticmethod
    def _check(data: Any) -> Any:
        """Raise ``HabrApiError`` if ``data`` is a Habr error response dict."""
        if isinstance(data, dict):
            http_code = data.get("httpCode")
            error_code = data.get("errorCode")
            if (isinstance(http_code, int) and http_code >= 400) or error_code:
                message = data.get("message") or "Habr API error"
                raise HabrApiError(str(message))
        return data

    def _auth_headers(self) -> dict[str, str]:
        """Build Cookie + csrf-token headers; raise if creds are missing."""
        sid = self._settings.habr_connect_sid
        token = self._settings.habr_csrf_token
        if not sid or not token:
            raise HabrApiError(MISSING_CREDS_MESSAGE)
        cookie_name = self._settings.habr_csrf_cookie_name
        cookie = f"connect.sid={sid}; {cookie_name}={token}"
        return {"Cookie": cookie, "csrf-token": token}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` with default lang params merged in; return parsed JSON."""
        merged = dict(self._default_params)
        if params:
            # Drop None values so optional filters (e.g. hub) are simply omitted.
            merged.update({k: v for k, v in params.items() if v is not None})
        try:
            response = await self._client.get(path, params=merged)
        except httpx.HTTPError as exc:
            raise HabrApiError(f"Сетевая ошибка при запросе к Habr: {exc}") from exc
        return self._parse(response)

    async def _post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        *,
        method: str = "POST",
        auth: bool = False,
    ) -> Any:
        """Send a POST/DELETE to ``path``; optionally with auth headers."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers())
        # Send default lang params here too (Habr expects them on writes).
        try:
            response = await self._client.request(
                method,
                path,
                params=dict(self._default_params),
                json=json if json is not None else {},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise HabrApiError(f"Сетевая ошибка при запросе к Habr: {exc}") from exc
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> Any:
        """Decode JSON and run the error-dict check; non-JSON bodies are errors."""
        try:
            data = response.json()
        except ValueError:
            # A successful write may legitimately return an empty body or
            # ``204 No Content``; treat that as success instead of an error.
            if response.is_success and not response.content.strip():
                return {}
            # HTML error pages like "Cannot POST ..." land here.
            snippet = response.text.strip().replace("\n", " ")[:200]
            raise HabrApiError(
                f"Habr вернул не-JSON ответ (HTTP {response.status_code}): {snippet}"
            )
        return self._check(data)

    # -- read methods -------------------------------------------------------

    async def list_articles(
        self,
        feed: str = "top",
        period: str = "daily",
        hub: str | None = None,
        page: int = 1,
    ) -> dict[str, Any]:
        """Fetch an article feed: ``top``, ``new`` or ``news``.

        ``sort=date`` (new/news) requires a ``period`` or Habr returns HTTP 422,
        so we always send it.
        """
        params: dict[str, Any] = {
            "page": page,
            "perPage": self._settings.per_page,
            "period": period,
        }
        if feed == "top":
            params["sort"] = "rating"
        elif feed == "new":
            params["sort"] = "date"
        elif feed == "news":
            params["news"] = "true"
            params["sort"] = "date"
        else:
            raise HabrApiError(f"Неизвестная лента: {feed}")
        if hub:
            params["hub"] = hub
        return await self._get("articles/", params)

    async def search_articles(self, query: str, page: int = 1) -> dict[str, Any]:
        """Full-text search over articles, sorted by relevance."""
        params: dict[str, Any] = {
            "query": query,
            "sort": "relevance",
            "page": page,
            "perPage": self._settings.per_page,
        }
        return await self._get("articles/", params)

    async def get_article(self, article_id: int) -> dict[str, Any]:
        """Fetch a single full article object (includes ``textHtml`` body)."""
        return await self._get(f"articles/{article_id}/")

    async def get_comments(self, article_id: int) -> dict[str, Any]:
        """Fetch the comment tree payload for an article."""
        return await self._get(f"articles/{article_id}/comments/")

    # -- write methods (require auth) --------------------------------------

    async def post_comment(
        self, article_id: int, text: str, parent_id: int = 0
    ) -> dict[str, Any]:
        """Post a comment; ``parent_id`` 0 = top-level, else a reply target."""
        body = {"text": _wrap_html(text), "parent_id": parent_id}
        return await self._post(
            f"articles/{article_id}/comments/add/", json=body, auth=True
        )

    @staticmethod
    def _check_direction(direction: str) -> None:
        """Reject anything but ``up``/``down`` before it reaches the URL path."""
        if direction not in ("up", "down"):
            raise HabrApiError("Направление должно быть 'up' или 'down'.")

    async def vote_article(self, article_id: int, direction: str) -> dict[str, Any]:
        """Vote on an article; direction (``up``/``down``) is in the URL path."""
        self._check_direction(direction)
        return await self._post(
            f"articles/{article_id}/votes/{direction}/", json={}, auth=True
        )

    async def vote_comment(self, comment_id: int, direction: str) -> dict[str, Any]:
        """Vote on a comment; direction is in the URL path.

        EXPERIMENTAL: route existence confirmed (401 without auth) but not
        exercised with a real logged-in session.
        """
        self._check_direction(direction)
        return await self._post(
            f"articles/comments/{comment_id}/votes/{direction}/", json={}, auth=True
        )

    async def bookmark_article(self, article_id: int, add: bool = True) -> dict[str, Any]:
        """Add or remove an article bookmark.

        POST (add) confirmed at the route level; DELETE (remove) is best-effort
        and EXPERIMENTAL — may need a route tweak.
        """
        method = "POST" if add else "DELETE"
        return await self._post(
            f"articles/{article_id}/bookmarks/", json={}, method=method, auth=True
        )

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
