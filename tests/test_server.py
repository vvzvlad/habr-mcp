"""Tests for the FastMCP server: tool registration and end-to-end tool calls.

Tools are invoked through FastMCP's ``call_tool`` so we exercise the real
registration path. httpx is mocked with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from src.client import HabrClient
from src.server import build_server

BASE_URL = "https://habr.com/kek/v2/"


def _text(result) -> str:
    """Extract the text from a FastMCP call_tool result across SDK versions.

    Newer FastMCP returns a (content, structured) tuple; older returns just the
    content list. Each content item is a TextContent with a ``.text`` attribute.
    """
    content = result[0] if isinstance(result, tuple) else result
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


async def test_tools_are_registered(auth_settings):
    server = build_server(auth_settings)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_articles",
        "list_articles",
        "get_article",
        "get_comments",
        "post_comment",
        "vote_article",
        "vote_comment",
        "bookmark_article",
    }


@respx.mock
async def test_list_articles_tool_returns_formatted(anon_settings, feed_payload):
    respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    server = build_server(anon_settings)
    result = await server.call_tool(
        "list_articles", {"feed": "top", "period": "daily"}
    )
    out = _text(result)
    assert "Вторая статья" in out
    assert "Первая" in out
    assert "id=200" in out


@respx.mock
async def test_get_article_tool(anon_settings, article_payload):
    respx.get(f"{BASE_URL}articles/100/").mock(
        return_value=httpx.Response(200, json=article_payload)
    )
    server = build_server(anon_settings)
    result = await server.call_tool("get_article", {"article_id": 100})
    out = _text(result)
    assert "# Заголовок статьи" in out
    assert "## Раздел" in out


async def test_list_articles_tool_rejects_bad_feed(anon_settings):
    server = build_server(anon_settings)
    result = await server.call_tool("list_articles", {"feed": "bogus"})
    out = _text(result)
    assert "Недопустимый feed" in out
    assert "top" in out


async def test_vote_article_tool_rejects_bad_direction(auth_settings):
    server = build_server(auth_settings)
    result = await server.call_tool(
        "vote_article", {"article_id": 100, "direction": "sideways"}
    )
    out = _text(result)
    assert "Недопустимый direction" in out


async def test_write_tool_without_creds_returns_russian_message(anon_settings):
    server = build_server(anon_settings)
    result = await server.call_tool(
        "vote_article", {"article_id": 100, "direction": "up"}
    )
    out = _text(result)
    assert "HABR_CONNECT_SID" in out
    assert "HABR_CSRF_TOKEN" in out


@respx.mock
async def test_vote_article_tool_with_creds(auth_settings):
    respx.post(f"{BASE_URL}articles/100/votes/up/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    server = build_server(auth_settings)
    result = await server.call_tool(
        "vote_article", {"article_id": 100, "direction": "up"}
    )
    out = _text(result)
    assert "Голос за статью учтён" in out


async def test_lifespan_closes_http_client(auth_settings, monkeypatch):
    # The registered lifespan must close the long-lived httpx client on teardown.
    closed = {"called": False}
    real_aclose = HabrClient.aclose

    async def spy_aclose(self):
        closed["called"] = True
        await real_aclose(self)

    monkeypatch.setattr(HabrClient, "aclose", spy_aclose)

    server = build_server(auth_settings)
    lifespan = server.settings.lifespan
    assert lifespan is not None
    async with lifespan(server):
        assert closed["called"] is False  # not closed while running
    # Once the lifespan context exits, the client must have been closed.
    assert closed["called"] is True
