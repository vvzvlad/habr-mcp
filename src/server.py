"""FastMCP server wiring: HTTP-only, multi-tenant Habr MCP server.

Identity is an opaque bearer token the user puts in their MCP client config
header (``Authorization: Bearer <token>``). The token is self-asserted: the
first time it is seen it is a fresh empty identity, and per token we store that
user's Habr credentials. There is NO stdio mode and NO global single-user
client — every Habr tool is routed through the token's own ``HabrClient``.

The full static tool list is ALWAYS exposed; access is gated by return messages,
never by hiding tools. Three states:
  * ANON         — no/blank bearer token; tools return a "paste this key" guide.
  * NEEDS_LOGIN  — valid token but no stored Habr creds; tools ask to habr_login.
  * READY        — token + stored creds; tools work via that user's HabrClient.

Tool descriptions are in Russian (LLM-facing); code/comments are in English.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from src.client import HabrApiError, HabrClient, fetch_csrf_token
from src.formatting import (
    format_article,
    format_article_list,
    format_comments,
    format_draft,
)
from src.registry import ClientRegistry
from src.settings import Settings
from src.settings import settings as _default_settings
from src.store import CredStore, derive_uuid_from_cookie, generate_key

# Allowed enum values, reused for validation and error messages.
FEEDS = ("top", "new", "news")
PERIODS = ("daily", "weekly", "monthly", "yearly", "alltime")
DIRECTIONS = ("up", "down")

# Auth states returned by ``resolve``.
ANON = "ANON"
NEEDS_LOGIN = "NEEDS_LOGIN"
READY = "READY"


# -- auth helpers (pure, testable) -----------------------------------------


def extract_bearer(headers: Mapping[str, str] | None) -> str | None:
    """Pull the bearer token out of an ``Authorization`` header (any case).

    Returns the raw token, or None when the header is missing/blank. A bare
    ``Bearer`` with no value, or a blank token, yields None.
    """
    if not headers:
        return None
    value: str | None = None
    for name, raw in headers.items():
        if name.lower() == "authorization":
            value = raw
            break
    if not value:
        return None
    token = value.strip()
    # Strip a leading ``Bearer`` scheme (any case), with or without trailing
    # content; ``Bearer`` alone leaves an empty token -> None.
    if token.lower() == "bearer":
        return None
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return token or None


def token_from_ctx(ctx: Context | None) -> str | None:
    """Read the bearer token from the request behind a FastMCP ``Context``.

    Defensive against a missing request: ``ctx.request_context`` RAISES when
    there is no active request (e.g. ``call_tool`` invoked directly in tests),
    and ``.request`` can be None on non-HTTP transports. Any such case yields
    None, which the caller treats as ANON.
    """
    if ctx is None:
        return None
    try:
        request = ctx.request_context.request
    except (ValueError, AttributeError):
        return None
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    return extract_bearer(headers)


async def resolve(
    ctx: Context | None, store: CredStore, registry: ClientRegistry
) -> tuple[str, str | None, HabrClient | None]:
    """Classify the current request into (state, token, client).

    * No/blank token            -> (ANON, None, None)
    * Token but no stored creds  -> (NEEDS_LOGIN, token, None)
    * Token with stored creds    -> (READY, token, <client>)
    """
    token = token_from_ctx(ctx)
    if not token:
        return ANON, None, None
    client = await registry.get(token)
    if client is None:
        return NEEDS_LOGIN, token, None
    return READY, token, client


def anon_message() -> str:
    """Guidance for an anonymous caller: generate and show a fresh key to paste."""
    key = generate_key()
    return (
        "🔑 Нужен личный ключ. Добавь эту строку в раздел \"headers\" твоего "
        "MCP-клиента и переподключись:\n\n"
        f"Authorization: Bearer {key}\n\n"
        "Это твой секретный ключ — храни как пароль. После переподключения "
        "вызови habr_login."
    )


def needs_login_message() -> str:
    """Guidance for a known token without stored Habr credentials."""
    return (
        "Ключ принят, но Habr-логин ещё не сохранён. Вызови habr_login и передай "
        "полный заголовок Cookie из залогиненного браузера.\n"
        "Как взять: DevTools → Network → любой запрос к habr.com/kek/v2/ → Copy → "
        "значение заголовка Cookie."
    )


def _warnings_suffix(warnings: list[str] | None) -> str:
    """Render a 'Предупреждения:' bullet block, or empty string if none."""
    if not warnings:
        return ""
    bullets = "\n".join(f"- {w}" for w in warnings)
    return f"\nПредупреждения:\n{bullets}"


def _draft_id(response: Any) -> str:
    """Best-effort extraction of the new draft id from a save response.

    The live create response is ``{"post":"<id>","ok":true}``, so ``post`` is
    checked first; the others remain as fallbacks for older/other shapes.
    """
    if isinstance(response, dict):
        for key in ("post", "id", "publicationId", "postId"):
            value = response.get(key)
            if value:
                return str(value)
        data = response.get("data")
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    return "?"


def build_server(settings: Settings | None = None) -> FastMCP:
    """Build the HTTP-only, multi-tenant FastMCP server.

    A base ``Settings`` carries shared config; a ``CredStore`` persists per-token
    Habr credentials and a ``ClientRegistry`` builds one ``HabrClient`` per
    token. No global Habr client exists. The full static tool list is registered
    regardless of auth state — gating happens inside each tool via return value.
    """
    base_settings = settings or _default_settings
    store = CredStore(base_settings.state_dir)
    registry = ClientRegistry(base_settings, store)

    @asynccontextmanager
    async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
        """Close every per-user httpx client when the HTTP server shuts down."""
        try:
            yield
        finally:
            await registry.aclose_all()

    mcp = FastMCP(
        "habr",
        host=base_settings.host,
        port=base_settings.port,
        lifespan=_lifespan,
    )

    async def _ready_client(ctx: Context) -> tuple[HabrClient | None, str | None]:
        """Resolve the caller; return (client, None) when READY else (None, guard).

        The guard message tells the LLM exactly what the user must do next
        (paste a key, or call habr_login). Used by every Habr tool.
        """
        state, _token, client = await resolve(ctx, store, registry)
        if state == READY and client is not None:
            return client, None
        if state == NEEDS_LOGIN:
            return None, needs_login_message()
        return None, anon_message()

    # -- read tools ---------------------------------------------------------

    @mcp.tool(
        name="search_articles",
        description=(
            "Поиск статей на Habr по тексту запроса (сортировка по релевантности). "
            "Аргумент query — поисковая строка. Аргумент page — номер страницы "
            "(по умолчанию 1). Возвращает нумерованный список статей с id, автором, "
            "датой, рейтингом и хабами."
        ),
    )
    async def search_articles(query: str, ctx: Context, page: int = 1) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            payload = await client.search_articles(query, page)
        except HabrApiError as exc:
            return str(exc)
        return format_article_list(payload, f'Результаты поиска: "{query}" (стр. {page})')

    @mcp.tool(
        name="list_articles",
        description=(
            "Лента статей Habr. Аргумент feed: 'top' (по рейтингу), 'new' "
            "(новые статьи) или 'news' (новости). Аргумент period: daily, weekly, "
            "monthly, yearly или alltime (по умолчанию daily). Аргумент hub — "
            "необязательный алиас хаба для фильтрации. Аргумент page — номер "
            "страницы. Возвращает нумерованный список статей."
        ),
    )
    async def list_articles(
        ctx: Context,
        feed: str = "top",
        period: str = "daily",
        hub: str | None = None,
        page: int = 1,
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        if feed not in FEEDS:
            return f"Недопустимый feed='{feed}'. Допустимо: {', '.join(FEEDS)}."
        if period not in PERIODS:
            return f"Недопустимый period='{period}'. Допустимо: {', '.join(PERIODS)}."
        try:
            payload = await client.list_articles(feed, period, hub, page)
        except HabrApiError as exc:
            return str(exc)
        hub_suffix = f", хаб {hub}" if hub else ""
        header = f"Лента '{feed}' (период {period}{hub_suffix}, стр. {page})"
        return format_article_list(payload, header)

    @mcp.tool(
        name="get_article",
        description=(
            "Полный текст одной статьи Habr по её числовому id. Аргумент "
            "article_id — id статьи. Возвращает метаданные (заголовок, автор, "
            "рейтинг, хабы, теги, ссылка) и тело статьи в Markdown."
        ),
    )
    async def get_article(article_id: int, ctx: Context) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            data = await client.get_article(article_id)
        except HabrApiError as exc:
            return str(exc)
        return format_article(data)

    @mcp.tool(
        name="get_comments",
        description=(
            "Комментарии к статье Habr в виде дерева с отступами по уровню "
            "вложенности. Аргумент article_id — id статьи. Аргумент limit — "
            "максимум комментариев в выводе (по умолчанию 100). Показывает "
            "автора, дату, рейтинг и текст каждого комментария."
        ),
    )
    async def get_comments(article_id: int, ctx: Context, limit: int = 100) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            payload = await client.get_comments(article_id)
        except HabrApiError as exc:
            return str(exc)
        return format_comments(payload, limit)

    # -- write tools (require login) ---------------------------------------

    @mcp.tool(
        name="post_comment",
        description=(
            "Опубликовать комментарий к статье Habr (требует сохранённого логина — "
            "вызови habr_login). Аргумент article_id — id статьи. Аргумент text — "
            "текст комментария (обычный текст обернётся в HTML автоматически). "
            "Аргумент parent_id — id комментария для ответа, либо пусто/0 для "
            "комментария верхнего уровня."
        ),
    )
    async def post_comment(
        article_id: int, text: str, ctx: Context, parent_id: int | None = None
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            result = await client.post_comment(article_id, text, parent_id or 0)
        except HabrApiError as exc:
            return str(exc)
        return f"Комментарий отправлен. Ответ Habr: {result}"

    @mcp.tool(
        name="vote_article",
        description=(
            "Проголосовать за статью Habr (требует сохранённого логина — вызови "
            "habr_login). Аргумент article_id — id статьи. Аргумент direction — "
            "'up' (плюс) или 'down' (минус)."
        ),
    )
    async def vote_article(article_id: int, direction: str, ctx: Context) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        if direction not in DIRECTIONS:
            return f"Недопустимый direction='{direction}'. Допустимо: up, down."
        try:
            result = await client.vote_article(article_id, direction)
        except HabrApiError as exc:
            return str(exc)
        return f"Голос за статью учтён. Ответ Habr: {result}"

    @mcp.tool(
        name="vote_comment",
        description=(
            "Проголосовать за комментарий Habr (требует сохранённого логина — "
            "вызови habr_login). Нужны оба id: article_id — id статьи, "
            "comment_id — id комментария. Аргумент direction — 'up' (плюс) или "
            "'down' (минус)."
        ),
    )
    async def vote_comment(
        article_id: int, comment_id: int, direction: str, ctx: Context
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        if direction not in DIRECTIONS:
            return f"Недопустимый direction='{direction}'. Допустимо: up, down."
        try:
            result = await client.vote_comment(article_id, comment_id, direction)
        except HabrApiError as exc:
            return str(exc)
        return f"Голос за комментарий учтён. Ответ Habr: {result}"

    # -- author tools: drafts (require a logged-in author session) ----------

    @mcp.tool(
        name="create_draft",
        description=(
            "Создать черновик статьи на Habr из страницы Docmost (требует "
            "сохранённого авторского логина — вызови habr_login). Аргумент title — "
            "заголовок. Аргумент doc — ProseMirror-JSON документа Docmost (как "
            "отдаёт get_page_json), строкой. Habr ТРЕБУЕТ: hubs — минимум один "
            "числовой id хаба (резолвьте алиасы через resolve_hubs); tags — минимум "
            "один тег; flow — обязательный id потока (см. list_flows). Аргумент "
            "announce — анонс «до ката», 100–3000 символов; если не передан, "
            "берётся из текста статьи (если получится короче 100 символов — будет "
            "ошибка). Аргумент format — формат поста (по умолчанию 'common'). "
            "Возвращает id созданного черновика и предупреждения конвертации."
        ),
    )
    async def create_draft(
        title: str,
        doc: str,
        ctx: Context,
        hubs: list[str] | None = None,
        tags: list[str] | None = None,
        flow: str | None = None,
        announce: str | None = None,
        format: str = "common",
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            parsed_doc = json.loads(doc)
        except (ValueError, TypeError) as exc:
            return f"Не удалось разобрать doc как JSON: {exc}"
        try:
            result = await client.create_draft(
                title,
                parsed_doc,
                hubs=hubs,
                tags=tags,
                flow=flow,
                announce=announce,
                fmt=format,
            )
        except HabrApiError as exc:
            return str(exc)
        draft_id = _draft_id(result.get("response"))
        return (
            f"Черновик создан. id={draft_id}."
            + _warnings_suffix(result.get("warnings"))
        )

    @mcp.tool(
        name="get_draft",
        description=(
            "Прочитать черновик/пост Habr по id (требует авторского логина — вызови "
            "habr_login). Аргумент post_id — id черновика. Возвращает сводку "
            "(заголовок, статус, хабы, теги, формат) и сырые ProseMirror-исходники "
            "text/preview для последующей правки."
        ),
    )
    async def get_draft(post_id: int, ctx: Context) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            return format_draft(await client.get_draft(post_id))
        except HabrApiError as exc:
            return str(exc)

    @mcp.tool(
        name="update_draft",
        description=(
            "Обновить существующий черновик Habr (read-modify-write автосейв; "
            "требует авторского логина — вызови habr_login). Аргумент post_id — id "
            "черновика. Все остальные аргументы необязательны и перезаписывают "
            "соответствующие поля: title, doc (ProseMirror-JSON страницы Docmost "
            "строкой, как get_page_json), hubs, tags, flow, format. Аргумент "
            "announce — анонс «до ката» (100–3000 символов); переопределяет анонс, "
            "который иначе берётся из текста статьи. Возвращает результат и "
            "предупреждения конвертации."
        ),
    )
    async def update_draft(
        post_id: int,
        ctx: Context,
        title: str | None = None,
        doc: str | None = None,
        hubs: list[str] | None = None,
        tags: list[str] | None = None,
        flow: str | None = None,
        announce: str | None = None,
        format: str | None = None,
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        parsed_doc = None
        if doc is not None:
            try:
                parsed_doc = json.loads(doc)
            except (ValueError, TypeError) as exc:
                return f"Не удалось разобрать doc как JSON: {exc}"
        try:
            result = await client.update_draft(
                post_id,
                title=title,
                docmost_doc=parsed_doc,
                hubs=hubs,
                tags=tags,
                flow=flow,
                announce=announce,
                fmt=format,
            )
        except HabrApiError as exc:
            return str(exc)
        return (
            f"Черновик {post_id} сохранён."
            + _warnings_suffix(result.get("warnings"))
        )

    @mcp.tool(
        name="delete_draft",
        description=(
            "Удалить черновик Habr по id (требует авторского логина — вызови "
            "habr_login). Аргумент post_id — id черновика."
        ),
    )
    async def delete_draft(post_id: int, ctx: Context) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            result = await client.delete_draft(post_id)
        except HabrApiError as exc:
            return str(exc)
        return f"Черновик {post_id} удалён. Ответ Habr: {result}"

    @mcp.tool(
        name="resolve_hubs",
        description=(
            "Сопоставить человекочитаемые алиасы хабов их числовым id через каталог "
            "Habr (требует авторского логина — вызови habr_login). Аргумент "
            "aliases — список алиасов хабов. Аргумент post_id — необязательный id "
            "поста (контекст). Возвращает для каждого алиаса строку "
            "'alias → id (title)' или 'alias → не найден'."
        ),
    )
    async def resolve_hubs(
        aliases: list[str], ctx: Context, post_id: int | None = None
    ) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            catalog = await client.suggest_hubs(post_id)
        except HabrApiError as exc:
            return str(exc)
        alias_map: dict[str, dict[str, Any]] = {}
        for group in ("collective", "offtopic", "corporative", "byPost"):
            for hub in catalog.get(group) or []:
                if isinstance(hub, dict) and hub.get("alias"):
                    alias_map.setdefault(str(hub["alias"]), hub)
        lines: list[str] = []
        for alias in aliases:
            hub = alias_map.get(alias)
            if hub:
                title = hub.get("title") or hub.get("titleHtml") or ""
                lines.append(f"{alias} → {hub.get('id')} ({title})")
            else:
                lines.append(f"{alias} → не найден")
        return "\n".join(lines) if lines else "Не передано ни одного алиаса."

    @mcp.tool(
        name="list_flows",
        description=(
            "Список потоков (flows) Habr с их id и алиасами (требует авторского "
            "логина — вызови habr_login). Аргумент publication_id — необязательный "
            "id публикации (контекст). Используйте id потока в аргументе flow "
            "инструментов create_draft / update_draft."
        ),
    )
    async def list_flows(ctx: Context, publication_id: int | None = None) -> str:
        client, msg = await _ready_client(ctx)
        if msg:
            return msg
        try:
            data = await client.list_flows(publication_id)
        except HabrApiError as exc:
            return str(exc)
        flows = data.get("flows") or []
        lines: list[str] = []
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            lines.append(
                f"{flow.get('id')}  {flow.get('alias')}  {flow.get('title', '')}"
            )
        return "\n".join(lines) if lines else "Потоки не найдены."

    # -- auth tools ---------------------------------------------------------

    @mcp.tool(
        name="habr_login",
        description=(
            "Сохранить Habr-логин для твоего ключа. Передай cookie — ПОЛНЫЙ "
            "заголовок Cookie из залогиненного браузера (DevTools → Network → любой "
            "запрос к habr.com → Copy → значение заголовка Cookie). csrf_token "
            "необязателен: если не передан, я попробую вытащить его сам со страницы "
            "ленты. После успеха становятся доступны все инструменты Хабра."
        ),
    )
    async def habr_login(cookie: str, ctx: Context, csrf_token: str | None = None) -> str:
        state, token, _client = await resolve(ctx, store, registry)
        if state == ANON or not token:
            return anon_message()
        uuid = derive_uuid_from_cookie(cookie)
        csrf = csrf_token or await fetch_csrf_token(cookie, base_settings)
        if not csrf:
            return (
                "Не удалось автоматически определить csrf-токен. Передай его "
                "явным аргументом csrf_token (значение заголовка csrf-token из "
                "DevTools)."
            )
        store.set(token, cookie, csrf, uuid)
        # Drop any cached client built from the OLD creds so the next tool call
        # rebuilds it from the freshly stored Cookie (e.g. after re-login).
        await registry.invalidate(token)
        return "Куки сохранены, теперь доступны все инструменты Хабра."

    @mcp.tool(
        name="auth_status",
        description=(
            "Показать текущее состояние авторизации для твоего ключа: нет ключа / "
            "есть ключ, но нет логина / всё готово."
        ),
    )
    async def auth_status(ctx: Context) -> str:
        state, token, _client = await resolve(ctx, store, registry)
        if state == ANON:
            return (
                "Ключ не задан. Добавь Authorization: Bearer <ключ> в headers "
                "MCP-клиента и вызови habr_login."
            )
        if state == NEEDS_LOGIN:
            return "Ключ есть, но Habr-логин не сохранён. Вызови habr_login."
        # Never reveal any character of the secret token. Show a short,
        # non-reversible fingerprint (sha256 prefix) so the user can still tell
        # which key is active without leaking the key itself.
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:8] if token else "????????"
        return f"Готово: Habr-логин активен (ключ {fingerprint}…)."

    return mcp
