"""FastMCP server wiring: build the server and register the 8 Habr tools.

Tool descriptions are in Russian (LLM-facing), code/comments in English. Each
tool wraps the client call in ``try/except HabrApiError`` and returns a plain
string so the LLM always gets a clean result instead of a traceback.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from src.client import HabrApiError, HabrClient
from src.formatting import (
    format_article,
    format_article_list,
    format_comments,
    format_draft,
)
from src.settings import Settings

# Allowed enum values, reused for validation and error messages.
FEEDS = ("top", "new", "news")
PERIODS = ("daily", "weekly", "monthly", "yearly", "alltime")
DIRECTIONS = ("up", "down")


def _warnings_suffix(warnings: list[str] | None) -> str:
    """Render a 'Предупреждения:' bullet block, or empty string if none."""
    if not warnings:
        return ""
    bullets = "\n".join(f"- {w}" for w in warnings)
    return f"\nПредупреждения:\n{bullets}"


def _draft_id(response: Any) -> str:
    """Best-effort extraction of the new draft id from a save response."""
    if isinstance(response, dict):
        for key in ("id", "publicationId", "postId"):
            value = response.get(key)
            if value:
                return str(value)
        data = response.get("data")
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    return "?"


def build_server(settings: Settings | None = None) -> FastMCP:
    """Build a FastMCP server exposing Habr read + write tools.

    A single ``HabrClient`` is created and closed over by all tools.
    """
    settings = settings or Settings()
    client = HabrClient(settings)

    @asynccontextmanager
    async def _lifespan(_server: FastMCP) -> AsyncIterator[None]:
        """Close the long-lived httpx client when the stdio server shuts down."""
        try:
            yield
        finally:
            await client.aclose()

    mcp = FastMCP("habr", lifespan=_lifespan)

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
    async def search_articles(query: str, page: int = 1) -> str:
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
        feed: str = "top",
        period: str = "daily",
        hub: str | None = None,
        page: int = 1,
    ) -> str:
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
    async def get_article(article_id: int) -> str:
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
    async def get_comments(article_id: int, limit: int = 100) -> str:
        try:
            payload = await client.get_comments(article_id)
        except HabrApiError as exc:
            return str(exc)
        return format_comments(payload, limit)

    # -- write tools (require auth) ----------------------------------------

    @mcp.tool(
        name="post_comment",
        description=(
            "Опубликовать комментарий к статье Habr (требует залогиненной сессии: "
            "HABR_CONNECT_SID и HABR_CSRF_TOKEN). Аргумент article_id — id статьи. "
            "Аргумент text — текст комментария (обычный текст обернётся в HTML "
            "автоматически). Аргумент parent_id — id комментария для ответа, либо "
            "пусто/0 для комментария верхнего уровня."
        ),
    )
    async def post_comment(article_id: int, text: str, parent_id: int | None = None) -> str:
        try:
            result = await client.post_comment(article_id, text, parent_id or 0)
        except HabrApiError as exc:
            return str(exc)
        return f"Комментарий отправлен. Ответ Habr: {result}"

    @mcp.tool(
        name="vote_article",
        description=(
            "Проголосовать за статью Habr (требует залогиненной сессии). Аргумент "
            "article_id — id статьи. Аргумент direction — 'up' (плюс) или 'down' "
            "(минус)."
        ),
    )
    async def vote_article(article_id: int, direction: str) -> str:
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
            "ЭКСПЕРИМЕНТАЛЬНО: проголосовать за комментарий Habr (требует "
            "залогиненной сессии). Маршрут подтверждён, но не проверен с реальной "
            "сессией. Аргумент comment_id — id комментария. Аргумент direction — "
            "'up' или 'down'."
        ),
    )
    async def vote_comment(comment_id: int, direction: str) -> str:
        if direction not in DIRECTIONS:
            return f"Недопустимый direction='{direction}'. Допустимо: up, down."
        try:
            result = await client.vote_comment(comment_id, direction)
        except HabrApiError as exc:
            return str(exc)
        return f"Голос за комментарий учтён. Ответ Habr: {result}"

    @mcp.tool(
        name="bookmark_article",
        description=(
            "Добавить статью Habr в закладки или убрать из закладок (требует "
            "залогиненной сессии). Аргумент article_id — id статьи. Аргумент add — "
            "True добавить (по умолчанию), False убрать. Удаление помечено как "
            "экспериментальное."
        ),
    )
    async def bookmark_article(article_id: int, add: bool = True) -> str:
        try:
            result = await client.bookmark_article(article_id, add)
        except HabrApiError as exc:
            return str(exc)
        verb = "добавлена в закладки" if add else "убрана из закладок"
        return f"Статья {verb}. Ответ Habr: {result}"

    # -- author tools: drafts (require an author session) -------------------

    @mcp.tool(
        name="create_draft",
        description=(
            "Создать черновик статьи на Habr из страницы Docmost (требует "
            "залогиненной авторской сессии: HABR_COOKIE и HABR_CSRF_TOKEN). "
            "Аргумент title — заголовок. Аргумент doc — ProseMirror-JSON документа "
            "Docmost (как отдаёт get_page_json), строкой. Аргумент hubs — список "
            "id или алиасов хабов (резолвьте алиасы через resolve_hubs). Аргумент "
            "tags — список тегов. Аргумент flow — id потока (см. list_flows). "
            "Аргумент format — формат поста (по умолчанию 'common'). Возвращает id "
            "созданного черновика и предупреждения конвертации."
        ),
    )
    async def create_draft(
        title: str,
        doc: str,
        hubs: list[str] | None = None,
        tags: list[str] | None = None,
        flow: str | None = None,
        format: str = "common",
    ) -> str:
        try:
            parsed_doc = json.loads(doc)
        except (ValueError, TypeError) as exc:
            return f"Не удалось разобрать doc как JSON: {exc}"
        try:
            result = await client.create_draft(
                title, parsed_doc, hubs=hubs, tags=tags, flow=flow, fmt=format
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
            "Прочитать черновик/пост Habr по id (требует авторской сессии: "
            "HABR_COOKIE и HABR_CSRF_TOKEN). Аргумент post_id — id черновика. "
            "Возвращает сводку (заголовок, статус, хабы, теги, формат) и сырые "
            "ProseMirror-исходники text/preview для последующей правки."
        ),
    )
    async def get_draft(post_id: int) -> str:
        try:
            return format_draft(await client.get_draft(post_id))
        except HabrApiError as exc:
            return str(exc)

    @mcp.tool(
        name="update_draft",
        description=(
            "Обновить существующий черновик Habr (read-modify-write автосейв; "
            "требует авторской сессии: HABR_COOKIE и HABR_CSRF_TOKEN). Аргумент "
            "post_id — id черновика. Все остальные аргументы необязательны и "
            "перезаписывают соответствующие поля: title, doc (ProseMirror-JSON "
            "страницы Docmost строкой, как get_page_json), hubs, tags, flow, "
            "format. Возвращает результат и предупреждения конвертации."
        ),
    )
    async def update_draft(
        post_id: int,
        title: str | None = None,
        doc: str | None = None,
        hubs: list[str] | None = None,
        tags: list[str] | None = None,
        flow: str | None = None,
        format: str | None = None,
    ) -> str:
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
            "Удалить черновик Habr по id (требует авторской сессии: HABR_COOKIE и "
            "HABR_CSRF_TOKEN). Аргумент post_id — id черновика."
        ),
    )
    async def delete_draft(post_id: int) -> str:
        try:
            result = await client.delete_draft(post_id)
        except HabrApiError as exc:
            return str(exc)
        return f"Черновик {post_id} удалён. Ответ Habr: {result}"

    @mcp.tool(
        name="resolve_hubs",
        description=(
            "Сопоставить человекочитаемые алиасы хабов их числовым id через каталог "
            "Habr (требует авторской сессии: HABR_COOKIE и HABR_CSRF_TOKEN). "
            "Аргумент aliases — список алиасов хабов. Аргумент post_id — "
            "необязательный id поста (контекст). Возвращает для каждого алиаса "
            "строку 'alias → id (title)' или 'alias → не найден'."
        ),
    )
    async def resolve_hubs(aliases: list[str], post_id: int | None = None) -> str:
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
            "Список потоков (flows) Habr с их id и алиасами (требует авторской "
            "сессии: HABR_COOKIE и HABR_CSRF_TOKEN). Аргумент publication_id — "
            "необязательный id публикации (контекст). Используйте id потока в "
            "аргументе flow инструментов create_draft / update_draft."
        ),
    )
    async def list_flows(publication_id: int | None = None) -> str:
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

    return mcp
