"""FastMCP server wiring: build the server and register the 8 Habr tools.

Tool descriptions are in Russian (LLM-facing), code/comments in English. Each
tool wraps the client call in ``try/except HabrApiError`` and returns a plain
string so the LLM always gets a clean result instead of a traceback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from src.client import HabrApiError, HabrClient
from src.formatting import format_article, format_article_list, format_comments
from src.settings import Settings

# Allowed enum values, reused for validation and error messages.
FEEDS = ("top", "new", "news")
PERIODS = ("daily", "weekly", "monthly", "yearly", "alltime")
DIRECTIONS = ("up", "down")


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

    return mcp
