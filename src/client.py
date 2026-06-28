"""Async HTTP client for Habr's undocumented internal JSON API.

Everything Habr-route-specific (URLs, query params, request bodies, auth headers)
is centralized here so the routes are easy to adjust if Habr changes them. Read
methods are anonymous; write methods require a logged-in session, supplied
per-user by the registry from credentials stored via the ``habr_login`` tool
(full browser Cookie header + CSRF token).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import html as html_module
import re
import secrets
from os.path import splitext
from urllib.parse import unquote_to_bytes, urlsplit
from typing import Any

import httpx

from src.converter import (
    collect_image_srcs,
    docmost_to_habr_doc,
    image_src_key,
    make_preview_doc,
    serialize_source,
)
from src.gdoc_converter import gdoc_to_docmost_doc
from src.settings import Settings

# Base for every endpoint; trailing slash matters for httpx relative URL joins.
BASE_URL = "https://habr.com/kek/v2/"

# Message shown when a write tool is called without stored credentials.
MISSING_CREDS_MESSAGE = (
    "Нет сохранённой сессии Habr. Вызовите habr_login и передайте полный "
    "Cookie-заголовок залогиненного браузера."
)

# Message shown when an author tool (drafts) is called without author credentials.
AUTHOR_MISSING_CREDS_MESSAGE = (
    "Нет сохранённой авторской сессии Habr. Вызовите habr_login с полным "
    "Cookie-заголовком залогиненного браузера (connect_sid + hsec_id + "
    "habrsession_id + …); csrf-токен подтянется автоматически."
)

# Statuses a draft form may carry for update_draft to be safe. A falsy status
# (None/empty) means a brand-new/unknown form, which is also editable. Any other
# status (e.g. "published") means a live post we must NOT clobber via save.
_EDITABLE_DRAFT_STATUSES = {"drafted", "draft"}

# nanoid alphabet (URL-safe), used for the create-draft idempotenceKey.
_NANOID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"

# Matches a habrastorage URL anywhere in the upload response body (fallback).
_HABRASTORAGE_RE = re.compile(r"https://habrastorage\.org/\S+")

# The csrf token Habr embeds in the feed page lives in a meta tag. Tolerate
# single or double quotes and intermediate attributes (e.g. an `id=…` between
# name and content), matching both attribute orders (name-then-content and
# content-then-name).
_CSRF_META_RE = re.compile(
    r'<meta[^>]*\bname=["\']csrf-token["\'][^>]*\bcontent=["\']([^"\']+)["\']'
)
_CSRF_META_RE_REV = re.compile(
    r'<meta[^>]*\bcontent=["\']([^"\']+)["\'][^>]*\bname=["\']csrf-token["\']'
)

# The csrf-token meta lives on the logged-in feed page. The URL language segment
# MUST match the session's interface language (the `hl` cookie): requesting the
# wrong language (e.g. /ru/feed/ for an `hl=en` session) triggers a 302 to the
# other language, and httpx drops the manually-set Cookie header across that
# redirect — so the redirected page loads anonymously and carries no csrf meta.
_CSRF_FEED_URL_TEMPLATE = "https://habr.com/{lang}/feed/"
_CSRF_DEFAULT_LANG = "ru"

# A sane interface-language token (e.g. "ru", "en"); anything else falls back to
# the default so a malformed `hl` cookie cannot corrupt the probe URL path.
_LANG_RE = re.compile(r"^[a-z]{2}$")

# Habr rejects an announce (postForm.preview) shorter than this many rendered
# characters with HTTP 422 ("Аннотация не может быть короче 100 символов …").
_MIN_PREVIEW_CHARS = 100

# Upper bound for the announce. ``make_preview_doc`` already hard-caps the stored
# text at this length on a word boundary, but we reject an over-long announce up
# front so the caller gets a clear error instead of a silently truncated teaser.
_MAX_PREVIEW_CHARS = 3000


class HabrApiError(Exception):
    """Raised for any Habr API failure (HTTP error dict, bad body, transport)."""


# Pattern for an ETag that looks like a bare sha256 hex digest. The gitmost
# sandbox sends ``ETag: "<sha256hex>"`` for its blobs so habr can verify
# integrity; opaque CDN validators do NOT match and are never verified.
_SHA256_ETAG_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Image content type -> filename extension. Used to derive an upload filename
# when the source URL has no usable extension (e.g. sandbox blobs served at
# ``/api/sb/<uuid>`` with a correct Content-Type but no extension).
_IMAGE_EXT_BY_CONTENT_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}


def resource_link_uri(v: Any) -> str | None:
    """Return the ``uri`` of an MCP ``resource_link`` value, else None.

    A value is a link iff it is a dict with ``type == "resource_link"`` carrying
    a non-empty string ``uri``. A Docmost ProseMirror doc is also a dict but its
    ``type`` is ``"doc"``, so it is never mistaken for a link.
    """
    if isinstance(v, dict) and v.get("type") == "resource_link":
        uri = v.get("uri")
        if isinstance(uri, str) and uri:
            return uri
    return None


def _decode_data_uri(uri: str) -> tuple[bytes, str | None]:
    """Decode a ``data:[<mediatype>][;base64],<payload>`` URI to (bytes, media).

    Base64-decodes when ``;base64`` is present, otherwise percent-decodes the
    payload. Returns the bytes plus the declared media type (or None). Raises
    ``HabrApiError`` (Russian message) on a malformed data URI.
    """
    rest = uri[len("data:"):]
    if "," not in rest:
        raise HabrApiError("Некорректный data: URI (нет запятой-разделителя).")
    meta, payload = rest.split(",", 1)
    params = meta.split(";") if meta else []
    is_base64 = params and params[-1].strip().lower() == "base64"
    media_type = params[0].strip() if params and params[0].strip() else None
    if is_base64:
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise HabrApiError(f"Некорректный data: URI (base64): {exc}") from exc
    else:
        try:
            data = unquote_to_bytes(payload)
        except (ValueError, TypeError) as exc:
            raise HabrApiError(f"Некорректный data: URI: {exc}") from exc
    return data, media_type


def _upload_filename(uri: str, content_type: str | None) -> str:
    """Derive an upload filename for an image fetched from ``uri``.

    Keeps the URL path's own filename when it already has a real extension.
    Otherwise (e.g. a sandbox blob at ``/api/sb/<uuid>`` or a ``data:`` URI)
    derives the extension from the content type via ``_IMAGE_EXT_BY_CONTENT_TYPE``;
    falls back to ``image.png`` when the type is unknown.
    """
    if not uri.startswith("data:"):
        path = urlsplit(uri).path
        name = path.rsplit("/", 1)[-1]
        stem, ext = splitext(name)
        if stem and ext:
            return name
    ext = _IMAGE_EXT_BY_CONTENT_TYPE.get((content_type or "").lower())
    return f"image{ext}" if ext else "image.png"


def _validate_announce_length(text: str) -> None:
    """Raise ``HabrApiError`` unless the announce is 100..3000 chars long.

    The announce is a required, hand-written field (the «до ката» teaser); it is
    never derived from the article body. Shared by ``create_draft`` and the
    ``update_draft`` preview-update path so both surface the same clear Russian
    message instead of a raw Habr 422.
    """
    stripped = (text or "").strip()
    if len(stripped) < _MIN_PREVIEW_CHARS:
        raise HabrApiError(
            "Анонс (announce) обязателен и должен быть 100–3000 символов — "
            "это отдельное поле «до ката», напишите текст-тизер."
        )
    if len(stripped) > _MAX_PREVIEW_CHARS:
        raise HabrApiError(
            "Анонс (announce) слишком длинный (более 3000 символов) — "
            "это отдельное поле «до ката», сократите текст-тизер."
        )


def _wrap_html(text: str) -> str:
    """Habr expects HTML in comment bodies.

    If the caller's text already contains a tag, send it as-is; otherwise escape
    ``& < >`` and wrap it in a single paragraph.
    """
    if "<" in text:
        return text
    return "<p>" + html_module.escape(text, quote=False) + "</p>"


def _cookie_interface_lang(cookie: str) -> str:
    """Return the interface language (`hl` cookie) so the csrf probe hits the
    matching feed URL and avoids a language redirect. `hl` may be like 'en' or
    'en,ru' — take the first token. Defaults to 'ru' when absent."""
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip() == "hl":
            first = value.split(",")[0].strip()
            # Only accept a simple language code (e.g. "ru", "en"); a malformed
            # value like "en/feed" must not leak into the probe URL path.
            if _LANG_RE.fullmatch(first):
                return first
    return _CSRF_DEFAULT_LANG


async def _get_preserving_cookie(client, url, headers, max_hops=3):
    """GET `url`, manually following SAME-HOST redirects while re-sending
    `headers` (incl. Cookie). httpx strips a manually-set Cookie header on
    redirect; we re-send it, but ONLY while the target host matches the original
    so the session Cookie is never leaked to an external redirect target."""
    origin = urlsplit(url)
    current = url
    for _ in range(max_hops + 1):
        response = await client.get(current, headers=headers)
        if not response.is_redirect:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        nxt = str(response.url.join(location))
        nxt_parts = urlsplit(nxt)
        if (nxt_parts.scheme, nxt_parts.netloc) != (origin.scheme, origin.netloc):
            # Cross-origin redirect (host or scheme changed): do NOT forward the
            # session Cookie (a scheme downgrade would leak it over plaintext).
            # Stop here; the body carries no csrf meta and the caller gets None.
            return response
        current = nxt
    return response


async def fetch_csrf_token(cookie: str, settings: Settings) -> str | None:
    """Scrape the csrf-token meta from the logged-in feed page for a Cookie.

    The feed URL language matches the session (`hl` cookie) to avoid a language
    redirect that would strip the Cookie header; any redirect that still occurs
    is followed manually with the Cookie re-sent. Returns the token, or None.
    """
    headers = {"User-Agent": settings.user_agent, "Cookie": cookie}
    url = _CSRF_FEED_URL_TEMPLATE.format(lang=_cookie_interface_lang(cookie))
    # Retry the whole fetch twice. This intentionally also covers a clean 200
    # that carried no csrf meta (a transient anonymous-looking render), not only
    # an httpx.HTTPError — a second attempt can ride out such a transient miss.
    for _attempt in range(2):
        try:
            async with httpx.AsyncClient(
                timeout=settings.request_timeout,
                proxy=settings.proxy or None,
                follow_redirects=False,
            ) as client:
                response = await _get_preserving_cookie(client, url, headers)
        except httpx.HTTPError:
            continue
        body = response.text or ""
        match = _CSRF_META_RE.search(body) or _CSRF_META_RE_REV.search(body)
        if match:
            return match.group(1)
    return None


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
        """Build Cookie + csrf-token headers for comment/vote writes.

        In the multi-tenant model the user supplies the FULL Cookie header
        (``habr_cookie``), so that is preferred. The legacy single-``connect.sid``
        construction is kept as a fallback for callers that only set
        ``habr_connect_sid``. Raises if neither cookie source is available.
        """
        token = self._settings.habr_csrf_token
        cookie = self._settings.habr_cookie
        if cookie and token:
            return {"Cookie": cookie, "csrf-token": token}
        sid = self._settings.habr_connect_sid
        if not sid or not token:
            raise HabrApiError(MISSING_CREDS_MESSAGE)
        cookie_name = self._settings.habr_csrf_cookie_name
        cookie = f"connect.sid={sid}; {cookie_name}={token}"
        return {"Cookie": cookie, "csrf-token": token}

    def _author_headers(self, referer: str | None = None) -> dict[str, str]:
        """Build the header bundle for ``publication/…`` author endpoints.

        Author endpoints need the full browser Cookie header plus the csrf-token;
        see protocol §2. Raises if either is missing.
        """
        cookie = self._settings.habr_cookie
        token = self._settings.habr_csrf_token
        if not cookie or not token:
            raise HabrApiError(AUTHOR_MISSING_CREDS_MESSAGE)
        headers: dict[str, str] = {
            "Cookie": cookie,
            "csrf-token": token,
            "accept": "application/json, text/plain, */*",
            "x-app-version": self._settings.habr_x_app_version,
            "origin": "https://habr.com",
            "referer": referer or "https://habr.com/ru/article/edit/",
        }
        if self._settings.habr_user_uuid:
            headers["habr-user-uuid"] = self._settings.habr_user_uuid
        return headers

    def _nanoid(self) -> str:
        """Generate a 21-char URL-safe id for the create-draft idempotenceKey.

        Habr uses this key as duplicate protection on draft creation (protocol
        §4): re-sending the same key must not create a second draft.
        """
        return "".join(secrets.choice(_NANOID_ALPHABET) for _ in range(21))

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """GET ``path`` with default lang params merged in; return parsed JSON."""
        merged = dict(self._default_params)
        if params:
            # Drop None values so optional filters (e.g. hub) are simply omitted.
            merged.update({k: v for k, v in params.items() if v is not None})
        try:
            response = await self._client.get(
                path, params=merged, headers=extra_headers or None
            )
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
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """Send a POST/DELETE to ``path``; optionally with auth headers.

        ``auth=True`` adds the comment/vote headers; ``extra_headers`` carries the
        author-endpoint headers and is merged last.
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers())
        if extra_headers:
            headers.update(extra_headers)
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

    async def vote_comment(
        self, article_id: int, comment_id: int, direction: str
    ) -> dict[str, Any]:
        """Vote on a comment; direction (``up``/``down``) goes in the JSON body.

        Posts ``{"value": 1}`` (up) or ``{"value": -1}`` (down) to
        ``articles/<article_id>/comments/<comment_id>/votes`` — the route Habr's
        own UI hits (verified live, HTTP 200).
        """
        self._check_direction(direction)
        value = 1 if direction == "up" else -1
        return await self._post(
            f"articles/{article_id}/comments/{comment_id}/votes",
            json={"value": value},
            auth=True,
        )

    # -- author layer: drafts (publication/…) ------------------------------

    async def create_draft(
        self,
        title: str,
        docmost_doc: dict,
        *,
        hubs: list[Any] | None = None,
        tags: list[str] | None = None,
        flow: Any | None = None,
        announce: str | None = None,
        fmt: str = "common",
        lang: str | None = None,
        article_type: str = "simple",
        preview_doc: dict | None = None,
    ) -> dict[str, Any]:
        """Create a Habr draft from a Docmost ProseMirror doc (protocol §4.1).

        Images are reuploaded to habrastorage first; the text is then converted
        to Habr's editorVersion-2 tree. Returns ``{"response", "warnings"}``.

        Validates the fields Habr requires on create (hubs/tags/flow non-empty,
        announce 100..3000 chars) locally and raises ``HabrApiError`` with a
        clear Russian message before any network call, so the LLM gets actionable
        feedback instead of a raw 422. The announce is a separate, required field
        the caller writes — it is NEVER derived from the article body.
        """
        if not hubs:
            raise HabrApiError(
                "Habr требует минимум один хаб (hubs). Подберите числовой id "
                "через resolve_hubs."
            )
        if not tags:
            raise HabrApiError("Habr требует минимум один тег (tags).")
        if flow is None or not str(flow).strip():
            raise HabrApiError(
                "Habr требует указать поток (flow). Список — через list_flows "
                "(например '2' — backend)."
            )
        # The announce is required and hand-written; validate it before any image
        # / conversion work so a missing teaser fails fast and cheaply.
        if not preview_doc:
            if not (announce and announce.strip()):
                raise HabrApiError(
                    "Анонс (announce) обязателен и должен быть 100–3000 "
                    "символов — это отдельное поле «до ката», напишите "
                    "текст-тизер."
                )
            _validate_announce_length(announce)

        image_map, warnings = await self._reupload_images(docmost_doc)
        habr_doc = docmost_to_habr_doc(docmost_doc, image_map, warnings)
        source = serialize_source(habr_doc)
        if preview_doc:
            preview = serialize_source(preview_doc)
        else:
            preview = serialize_source(make_preview_doc(announce))
        form: dict[str, Any] = {
            "lang": lang or self._settings.habr_lang,
            "type": article_type,
            "title": title,
            "feedCover": None,
            "hubs": [str(h) for h in hubs],
            "tags": list(tags),
            "text": {"source": source, "editorVersion": 2, "isMarkdown": False},
            "preview": {"source": preview, "editorVersion": 2, "isMarkdown": False},
            "leadButtonText": "Читать далее",
            "isTranslation": False,
            "format": fmt,
            "isPlanned": False,
            "plannedDateTime": None,
            "translationSource": None,
            "originalAuthor": None,
            "isCompanyExperience": False,
            "flow": str(flow),
            "status": "drafted",
            "banner": None,
            "multiwidget": None,
            "idempotenceKey": self._nanoid(),
        }
        result = await self._post(
            "publication/save",
            json=form,
            extra_headers=self._author_headers(
                referer="https://habr.com/ru/articles/new/"
            ),
        )
        return {"response": result, "warnings": warnings}

    async def create_draft_from_gdoc(
        self,
        title: str,
        gdoc_doc: Any,
        *,
        hubs: list[Any] | None = None,
        tags: list[str] | None = None,
        flow: Any | None = None,
        announce: str | None = None,
        fmt: str = "common",
        lang: str | None = None,
        article_type: str = "simple",
        preview_doc: dict | None = None,
    ) -> dict[str, Any]:
        """Create a Habr draft from a Google Docs "Document" (readDocument json).

        Converts the Google Docs JSON to an intermediate Docmost-shaped doc and
        then delegates to ``create_draft`` (which reuploads images and runs the
        Docmost->Habr pipeline). Conversion warnings are merged ahead of the
        pipeline warnings so the caller sees both.
        """
        conv_warnings: list[str] = []
        docmost_doc = gdoc_to_docmost_doc(gdoc_doc, conv_warnings)
        result = await self.create_draft(
            title,
            docmost_doc,
            hubs=hubs,
            tags=tags,
            flow=flow,
            announce=announce,
            fmt=fmt,
            lang=lang,
            article_type=article_type,
            preview_doc=preview_doc,
        )
        result["warnings"] = conv_warnings + result.get("warnings", [])
        return result

    async def get_draft(self, post_id: int) -> dict[str, Any]:
        """Read a draft/post form via ``publication/post-data/<id>``."""
        return await self._get(
            f"publication/post-data/{post_id}",
            extra_headers=self._author_headers(
                referer=f"https://habr.com/ru/article/edit/{post_id}/"
            ),
        )

    async def get_me(self) -> dict[str, Any]:
        """Fetch the current logged-in user object via ``me`` (author session).

        Used to resolve the author's own alias for endpoints that require a
        ``user`` query param (e.g. the drafts list). Returns the parsed dict.
        """
        return await self._get("me", extra_headers=self._author_headers())

    async def list_drafts(
        self, page: int = 1, draft_type: str = "posts"
    ) -> dict[str, Any]:
        """List the logged-in author's drafts (confirmed live).

        Habr's drafts list lives at ``GET articles/drafts`` (NO trailing slash —
        ``articles/drafts/`` is a different route that 404s) and REQUIRES the
        author's own ``user`` alias plus a ``draftType`` (``posts`` carries the
        article drafts this server creates). The alias is resolved via ``me``.
        Returns the standard feed payload
        (``publicationIds`` / ``publicationRefs`` / ``pagesCount``).
        """
        me = await self.get_me()
        alias = me.get("alias") if isinstance(me, dict) else None
        if not alias:
            raise HabrApiError(
                "Не удалось определить логин текущего пользователя Habr (me.alias)."
            )
        params: dict[str, Any] = {
            "user": alias,
            "draftType": draft_type,
            "page": page,
            "perPage": self._settings.per_page,
        }
        return await self._get(
            "articles/drafts", params, extra_headers=self._author_headers()
        )

    async def update_draft(
        self,
        post_id: int,
        *,
        title: str | None = None,
        docmost_doc: dict | None = None,
        hubs: list[Any] | None = None,
        tags: list[str] | None = None,
        flow: Any | None = None,
        announce: str | None = None,
        fmt: str | None = None,
        preview_doc: dict | None = None,
    ) -> dict[str, Any]:
        """Read-modify-write a draft: load the form, apply overrides, autosave.

        Coerces the write-side types Habr expects (``hubs`` -> list[str],
        ``text``/``preview`` editorVersion -> int 2) since ``post-data`` returns
        them as ints/strings. Returns ``{"response", "warnings"}``.
        """
        data = await self.get_draft(post_id)
        form = dict(data.get("postForm") or data)

        # Refuse to edit a non-draft post: save/<id> accepts ANY post id, so
        # updating a published article would overwrite the live version. A falsy
        # status is a brand-new/unknown form and is allowed; anything set and not
        # in the draft allow-set is rejected before any network/conversion work.
        status = form.get("status")
        if status and status not in _EDITABLE_DRAFT_STATUSES:
            raise HabrApiError(
                f"Пост {post_id} имеет статус '{status}' — это не черновик. "
                "update_draft правит только черновики, чтобы не перезаписать "
                "опубликованную статью."
            )

        warnings: list[str] = []

        if title is not None:
            form["title"] = title
        if hubs is not None:
            form["hubs"] = [str(h) for h in hubs]
        if tags is not None:
            form["tags"] = list(tags)
        if flow is not None:
            form["flow"] = str(flow)
        if fmt is not None:
            form["format"] = fmt

        # Convert the body only when a new doc is supplied. The announce is a
        # separate field: changing the body alone never touches the preview.
        if docmost_doc is not None:
            image_map, warnings = await self._reupload_images(docmost_doc)
            habr_doc = docmost_to_habr_doc(docmost_doc, image_map, warnings)
            form["text"] = {
                "source": serialize_source(habr_doc),
                "editorVersion": 2,
                "isMarkdown": False,
            }

        # Update the preview ONLY when the caller supplies a new announce (or an
        # explicit preview_doc). When neither is given the existing preview from
        # the fetched form is kept verbatim — even if the body changed.
        if preview_doc is not None:
            form["preview"] = {
                "source": serialize_source(preview_doc),
                "editorVersion": 2,
                "isMarkdown": False,
            }
        elif announce is not None:
            # The announce is hand-written; validate its length (symmetric with
            # create_draft) so a too-short/too-long value raises a clear error
            # instead of a raw Habr 422.
            _validate_announce_length(announce)
            form["preview"] = {
                "source": serialize_source(make_preview_doc(announce)),
                "editorVersion": 2,
                "isMarkdown": False,
            }

        # Coerce read-side types to write-side on the whole form (post-data returns
        # hubs as ints and editorVersion as the string "2").
        if isinstance(form.get("hubs"), list):
            form["hubs"] = [str(h) for h in form["hubs"]]
        for zone in ("text", "preview"):
            block = form.get(zone)
            if isinstance(block, dict):
                block["editorVersion"] = 2

        # Habr rejects a repeated save with the same/absent idempotency key
        # (REQUEST_ALREADY_PROCESSED); send a fresh one per save like the editor does.
        form["idempotenceKey"] = self._nanoid()

        result = await self._post(
            f"publication/save/{post_id}",
            json=form,
            extra_headers=self._author_headers(
                referer=f"https://habr.com/ru/article/edit/{post_id}/"
            ),
        )
        return {"response": result, "warnings": warnings}

    async def update_draft_from_gdoc(
        self,
        post_id: int,
        *,
        title: str | None = None,
        gdoc_doc: Any | None = None,
        hubs: list[Any] | None = None,
        tags: list[str] | None = None,
        flow: Any | None = None,
        announce: str | None = None,
        fmt: str | None = None,
        preview_doc: dict | None = None,
    ) -> dict[str, Any]:
        """Update a Habr draft from a Google Docs "Document" (readDocument json).

        The body is converted only when ``gdoc_doc`` is supplied; otherwise the
        update proceeds without touching ``text`` (same semantics as
        ``update_draft`` with ``docmost_doc=None``). Conversion warnings are
        merged ahead of the pipeline warnings.
        """
        conv_warnings: list[str] = []
        docmost_doc = (
            gdoc_to_docmost_doc(gdoc_doc, conv_warnings)
            if gdoc_doc is not None
            else None
        )
        result = await self.update_draft(
            post_id,
            title=title,
            docmost_doc=docmost_doc,
            hubs=hubs,
            tags=tags,
            flow=flow,
            announce=announce,
            fmt=fmt,
            preview_doc=preview_doc,
        )
        result["warnings"] = conv_warnings + result.get("warnings", [])
        return result

    async def delete_draft(self, post_id: int) -> dict[str, Any]:
        """Delete a draft via ``DELETE articles/drafts/<id>/posts``."""
        return await self._post(
            f"articles/drafts/{post_id}/posts",
            json={},
            method="DELETE",
            extra_headers=self._author_headers(
                referer=f"https://habr.com/ru/article/edit/{post_id}/"
            ),
        )

    async def suggest_hubs(self, post_id: int | None = None) -> dict[str, Any]:
        """Fetch the hub catalog (alias <-> id) via ``publication/suggest-hubs``."""
        params: dict[str, Any] = {
            "publicationType": "topic",
            "postType": "simple",
            "postContext": "topic",
        }
        if post_id is not None:
            params["post"] = post_id
        return await self._get(
            "publication/suggest-hubs",
            params,
            extra_headers=self._author_headers(),
        )

    async def list_flows(self, publication_id: int | None = None) -> dict[str, Any]:
        """Fetch the flows reference via ``refs/flows/wysiwyg``."""
        params: dict[str, Any] = {}
        if publication_id is not None:
            params["publicationId"] = publication_id
        return await self._get(
            "refs/flows/wysiwyg",
            params,
            extra_headers=self._author_headers(),
        )

    async def upload_image(
        self, image_bytes: bytes, filename: str, content_type: str
    ) -> str | None:
        """Upload one image to habrastorage via ``publication/upload``.

        EXPERIMENTAL (protocol §6.3): the multipart field name and exact response
        keys were not captured, so we try several response shapes and fall back to
        a regex over the raw body. Returns the habrastorage URL or ``None`` on any
        failure (network errors are swallowed so a publish is not aborted).
        """
        # Author headers minus Content-Type so httpx sets the multipart boundary.
        headers = {
            k: v for k, v in self._author_headers().items() if k.lower() != "content-type"
        }
        headers["Accept"] = "application/json"
        try:
            response = await self._client.post(
                "publication/upload",
                params=dict(self._default_params),
                files={"file": (filename, image_bytes, content_type)},
                headers=headers,
            )
        except httpx.HTTPError:
            return None
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            url = data.get("url") or data.get("src")
            if not url and isinstance(data.get("data"), dict):
                url = data["data"].get("url")
            if isinstance(url, str) and url:
                return url
        match = _HABRASTORAGE_RE.search(response.text or "")
        return match.group(0) if match else None

    async def fetch_resource(self, uri: str) -> tuple[bytes, str | None]:
        """Resolve a ``data:`` URI or ``http(s)`` URL to ``(bytes, content_type)``.

        ``data:`` URIs are decoded locally (no network); the content type is the
        declared media type (e.g. ``image/jpeg``) or None when absent. ``http(s)``
        URLs are fetched with a plain GET — NO Authorization header is ever sent;
        the content type is the response ``Content-Type`` (normalized to the part
        before ``;``, stripped and lowercased) or None when absent. The habr
        proxy/timeout/UA are reused (mirrors ``fetch_csrf_token``). When the
        ``ETag`` response header is a bare sha256 hex digest the body is verified
        against it (this guards the gitmost sandbox blobs, which send
        ``ETag: "<sha256hex>"``); any other / absent ETag is left unverified so
        external images with opaque CDN validators keep working. Raises
        ``HabrApiError`` (Russian message) on a malformed data URI, a transport
        error, or a sha256 mismatch.
        """
        if uri.startswith("data:"):
            data, media = _decode_data_uri(uri)
            return data, media
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.request_timeout,
                proxy=self._settings.proxy or None,
                headers={"User-Agent": self._settings.user_agent},
            ) as client:
                response = await client.get(uri)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HabrApiError(f"Не удалось загрузить ресурс {uri}: {exc}") from exc
        body = response.content
        etag = (response.headers.get("ETag") or "").strip()
        if etag.startswith("W/"):
            etag = etag[2:].strip()
        etag = etag.strip('"')
        if _SHA256_ETAG_RE.match(etag):
            actual = hashlib.sha256(body).hexdigest()
            if actual.lower() != etag.lower():
                raise HabrApiError(
                    f"Целостность ресурса нарушена: {uri} (повреждённый или "
                    "обрезанный blob, sha256 не совпал с ETag)."
                )
        raw_ct = response.headers.get("Content-Type")
        content_type = raw_ct.split(";", 1)[0].strip().lower() if raw_ct else None
        return body, (content_type or None)

    async def _reupload_images(
        self, docmost_doc: dict
    ) -> tuple[dict[str, str], list[str]]:
        """Fetch every image source and reupload it to habrastorage.

        Returns ``(image_src_key -> habrastorage_url, warnings)``. Never raises:
        image problems must not abort publishing (the text still goes through and
        the converter drops images that have no mapped URL). Each ``attrs.src``
        may be a plain http(s) URL string, a ``data:`` URI string, or an MCP
        ``resource_link`` dict; all are resolved through ``fetch_resource`` with
        NO credentials. Sandbox source links are ephemeral (~1h TTL), so all
        bytes are fetched up front and concurrently before any upload.
        """
        srcs = collect_image_srcs(docmost_doc)
        mapping: dict[str, str] = {}
        warnings: list[str] = []
        if not srcs:
            return mapping, warnings

        # Phase 1: fetch ALL image bytes concurrently, up front.
        fetch_uris = [resource_link_uri(src) or src for src in srcs]
        results = await asyncio.gather(
            *(self.fetch_resource(uri) for uri in fetch_uris),
            return_exceptions=True,
        )

        # Phase 2: upload each successfully fetched blob to habrastorage.
        for src, uri, result in zip(srcs, fetch_uris, results):
            if isinstance(result, BaseException):
                warnings.append(f"image fetch failed: {uri} ({result})")
                continue
            data, fetched_ct = result
            content_type = fetched_ct or "application/octet-stream"
            filename = _upload_filename(uri, fetched_ct)

            new_url = await self.upload_image(data, filename, content_type)
            if new_url:
                mapping[image_src_key(src)] = new_url
            else:
                warnings.append(f"image upload failed: {uri}")

        return mapping, warnings

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
