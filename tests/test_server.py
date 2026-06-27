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
        "create_draft",
        "get_draft",
        "update_draft",
        "delete_draft",
        "resolve_hubs",
        "list_flows",
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


@respx.mock
async def test_resolve_hubs_tool_maps_aliases(author_settings):
    catalog = {
        "collective": [{"id": "23108", "alias": "smol", "title": "$mol *"}],
        "offtopic": [{"id": "19259", "alias": "closet", "title": "Closet"}],
        "corporative": [],
        "byPost": [{"id": "161", "alias": "habr", "title": "Habr"}],
    }
    respx.get(f"{BASE_URL}publication/suggest-hubs").mock(
        return_value=httpx.Response(200, json=catalog)
    )
    server = build_server(author_settings)
    result = await server.call_tool(
        "resolve_hubs", {"aliases": ["habr", "smol", "ghost"]}
    )
    out = _text(result)
    assert "habr → 161 (Habr)" in out
    assert "smol → 23108 ($mol *)" in out
    assert "ghost → не найден" in out


@respx.mock
async def test_create_draft_tool_reports_id(author_settings, docmost_doc):
    import json as json_module

    # Live create response shape: {"post":"<id>","ok":true}.
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "777", "ok": True})
    )
    server = build_server(author_settings)
    result = await server.call_tool(
        "create_draft",
        {
            "title": "T",
            "doc": json_module.dumps(docmost_doc),
            "hubs": ["161"],
            "tags": ["t1"],
            "flow": "2",
            "announce": "А" * 120,
        },
    )
    out = _text(result)
    assert "id=777" in out


def test_draft_id_reads_post_key():
    from src.server import _draft_id

    # The live create response uses "post" for the new id.
    assert _draft_id({"post": "123", "ok": True}) == "123"
    # Fallback keys still work.
    assert _draft_id({"id": "9"}) == "9"
    assert _draft_id({"data": {"id": "5"}}) == "5"
    assert _draft_id({"ok": True}) == "?"


async def test_create_draft_tool_rejects_bad_json(author_settings):
    server = build_server(author_settings)
    result = await server.call_tool(
        "create_draft", {"title": "T", "doc": "{not json"}
    )
    out = _text(result)
    assert "Не удалось разобрать doc" in out


async def test_author_tool_without_creds_returns_message(anon_settings, docmost_doc):
    import json as json_module

    # Required fields are supplied so the creds check (not field validation) is
    # what fires, returning the author-credentials message.
    server = build_server(anon_settings)
    result = await server.call_tool(
        "create_draft",
        {
            "title": "T",
            "doc": json_module.dumps(docmost_doc),
            "hubs": ["161"],
            "tags": ["t1"],
            "flow": "2",
            "announce": "А" * 120,
        },
    )
    out = _text(result)
    assert "HABR_COOKIE" in out
    assert "HABR_CSRF_TOKEN" in out


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
