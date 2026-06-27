"""Async HTTP client for Habr's undocumented internal JSON API.

Everything Habr-route-specific (URLs, query params, request bodies, auth headers)
is centralized here so the routes are easy to adjust if Habr changes them. Read
methods are anonymous; write methods require a logged-in session, supplied
per-user by the registry from credentials stored via the ``habr_login`` tool
(full browser Cookie header + CSRF token).
"""

from __future__ import annotations

import html as html_module
import re
import secrets
from urllib.parse import urljoin, urlsplit
from typing import Any

import httpx

from src.converter import (
    collect_image_srcs,
    docmost_to_habr_doc,
    make_preview_doc,
    preview_text,
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

# Page that reliably carries the csrf-token meta tag for a logged-in session.
_CSRF_PROBE_URL = "https://habr.com/ru/feed/"

# Habr rejects an announce (postForm.preview) shorter than this many rendered
# characters with HTTP 422 ("Аннотация не может быть короче 100 символов …").
_MIN_PREVIEW_CHARS = 100


class HabrApiError(Exception):
    """Raised for any Habr API failure (HTTP error dict, bad body, transport)."""


def _validate_announce_length(text: str) -> None:
    """Raise ``HabrApiError`` if the derived announce is shorter than the minimum.

    Shared by ``create_draft`` and the ``update_draft`` preview-rebuild path so
    both surface the same clear Russian message instead of a raw Habr 422.
    """
    if len(text) < _MIN_PREVIEW_CHARS:
        raise HabrApiError(
            "Анонс (preview) получился короче 100 символов — Habr "
            "отклонит. Передайте announce явно или увеличьте текст "
            "статьи."
        )


def _wrap_html(text: str) -> str:
    """Habr expects HTML in comment bodies.

    If the caller's text already contains a tag, send it as-is; otherwise escape
    ``& < >`` and wrap it in a single paragraph.
    """
    if "<" in text:
        return text
    return "<p>" + html_module.escape(text, quote=False) + "</p>"


async def fetch_csrf_token(cookie: str, settings: Settings) -> str | None:
    """Scrape the csrf-token from the Habr feed page for a logged-in Cookie.

    GETs ``https://habr.com/ru/feed/`` with the given Cookie header (browser-like
    UA, configured proxy/timeout) and reads the ``<meta name="csrf-token">`` tag.
    Returns the token, or None when it cannot be found or a network error occurs.
    """
    headers = {"User-Agent": settings.user_agent, "Cookie": cookie}
    try:
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            proxy=settings.proxy or None,
            follow_redirects=True,
        ) as client:
            response = await client.get(_CSRF_PROBE_URL, headers=headers)
    except httpx.HTTPError:
        return None
    body = response.text or ""
    match = _CSRF_META_RE.search(body) or _CSRF_META_RE_REV.search(body)
    return match.group(1) if match else None


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
        feedback instead of a raw 422.
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

        image_map, warnings = await self._reupload_images(docmost_doc)
        habr_doc = docmost_to_habr_doc(docmost_doc, image_map, warnings)
        source = serialize_source(habr_doc)
        if preview_doc:
            preview = serialize_source(preview_doc)
        else:
            _validate_announce_length(preview_text(habr_doc, announce))
            preview = serialize_source(make_preview_doc(habr_doc, announce))
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

        # Convert the body only when a new doc is supplied; keep a handle to it so
        # the announce can be derived from it below when no explicit announce/
        # preview_doc is given.
        habr_doc: dict | None = None
        if docmost_doc is not None:
            image_map, warnings = await self._reupload_images(docmost_doc)
            habr_doc = docmost_to_habr_doc(docmost_doc, image_map, warnings)
            form["text"] = {
                "source": serialize_source(habr_doc),
                "editorVersion": 2,
                "isMarkdown": False,
            }

        # Rebuild the preview when the body changed OR an announce/preview_doc was
        # given. An explicit preview_doc wins; otherwise build from the announce
        # (or, when only the body changed, from the new body text).
        if preview_doc is not None:
            form["preview"] = {
                "source": serialize_source(preview_doc),
                "editorVersion": 2,
                "isMarkdown": False,
            }
        elif docmost_doc is not None or announce is not None:
            # ``preview_text`` ignores the doc when announce is set, so an empty
            # doc is a safe stand-in for the announce-only path. Validate the
            # derived announce length here too (symmetric with create_draft) so a
            # too-short rebuild raises a clear error instead of a raw Habr 422.
            base_doc = habr_doc if habr_doc is not None else {"type": "doc", "content": []}
            _validate_announce_length(preview_text(base_doc, announce))
            form["preview"] = {
                "source": serialize_source(make_preview_doc(base_doc, announce)),
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

    async def _reupload_images(
        self, docmost_doc: dict
    ) -> tuple[dict[str, str], list[str]]:
        """Download every Docmost image and reupload it to habrastorage.

        Returns ``(src -> habrastorage_url, warnings)``. Never raises: image
        problems must not abort publishing (the text still goes through and the
        converter drops images that have no mapped URL).
        """
        srcs = collect_image_srcs(docmost_doc)
        mapping: dict[str, str] = {}
        warnings: list[str] = []
        token = self._settings.docmost_api_token
        base = self._settings.docmost_base_url
        # ``.hostname`` is lowercased and port-stripped, so the host match is
        # case-insensitive and ignores an explicit default port (e.g. ":443").
        base_host = urlsplit(base).hostname if base else None

        for src in srcs:
            # ``is_docmost`` tracks whether the resolved URL targets the Docmost
            # host, so the Docmost bearer token is sent ONLY there. A relative src
            # joined with the base is Docmost; an absolute URL is Docmost only when
            # its host matches the base host. Any other absolute URL (e.g. a Google
            # contentUri on googleusercontent.com, or any external image) is
            # downloaded WITHOUT the Authorization header.
            if src.startswith("http://") or src.startswith("https://"):
                abs_url = src
                is_docmost = bool(base_host) and urlsplit(src).hostname == base_host
            elif base:
                abs_url = urljoin(base if base.endswith("/") else base + "/", src.lstrip("/"))
                is_docmost = True
            else:
                warnings.append(f"image skipped (no docmost_base_url): {src}")
                continue

            dl_headers = (
                {"Authorization": f"Bearer {token}"} if token and is_docmost else {}
            )
            try:
                resp = await self._client.get(abs_url, headers=dl_headers)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                warnings.append(f"image download failed: {src} ({exc})")
                continue

            path = urlsplit(abs_url).path
            filename = path.rsplit("/", 1)[-1] or "image.png"
            content_type = resp.headers.get("content-type") or "application/octet-stream"
            content_type = content_type.split(";", 1)[0].strip()

            new_url = await self.upload_image(resp.content, filename, content_type)
            if new_url:
                mapping[src] = new_url
            else:
                warnings.append(f"image upload failed: {src}")

        return mapping, warnings

    # -- lifecycle ----------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()
