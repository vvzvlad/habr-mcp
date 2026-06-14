"""Tests for HabrClient: request shapes, error handling, and auth headers."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.client import MISSING_CREDS_MESSAGE, HabrApiError, HabrClient

BASE_URL = "https://habr.com/kek/v2/"


@respx.mock
async def test_list_articles_top_params(anon_settings, feed_payload):
    route = respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    client = HabrClient(anon_settings)
    try:
        await client.list_articles(feed="top", period="weekly")
    finally:
        await client.aclose()

    params = route.calls.last.request.url.params
    assert params["sort"] == "rating"
    assert params["period"] == "weekly"
    assert params["fl"] == "ru"
    assert params["hl"] == "ru"
    assert "news" not in params


@respx.mock
async def test_list_articles_new_params(anon_settings, feed_payload):
    route = respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    client = HabrClient(anon_settings)
    try:
        await client.list_articles(feed="new", period="daily")
    finally:
        await client.aclose()

    params = route.calls.last.request.url.params
    assert params["sort"] == "date"
    assert params["period"] == "daily"
    assert "news" not in params


@respx.mock
async def test_list_articles_news_and_hub_params(anon_settings, feed_payload):
    route = respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    client = HabrClient(anon_settings)
    try:
        await client.list_articles(feed="news", period="monthly", hub="python")
    finally:
        await client.aclose()

    params = route.calls.last.request.url.params
    assert params["news"] == "true"
    assert params["sort"] == "date"
    assert params["period"] == "monthly"
    assert params["hub"] == "python"


@respx.mock
async def test_search_articles_params(anon_settings, feed_payload):
    route = respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    client = HabrClient(anon_settings)
    try:
        await client.search_articles("mcp", page=2)
    finally:
        await client.aclose()

    params = route.calls.last.request.url.params
    assert params["query"] == "mcp"
    assert params["sort"] == "relevance"
    assert params["page"] == "2"


@respx.mock
async def test_get_article_and_comments(anon_settings, article_payload, comments_payload):
    respx.get(f"{BASE_URL}articles/100/").mock(
        return_value=httpx.Response(200, json=article_payload)
    )
    respx.get(f"{BASE_URL}articles/100/comments/").mock(
        return_value=httpx.Response(200, json=comments_payload)
    )
    client = HabrClient(anon_settings)
    try:
        article = await client.get_article(100)
        comments = await client.get_comments(100)
    finally:
        await client.aclose()

    assert article["id"] == "100"
    assert comments["threads"] == ["1"]


def test_check_raises_on_error_dict(anon_settings):
    client = HabrClient(anon_settings)
    with pytest.raises(HabrApiError) as exc:
        client._check({"httpCode": 404, "message": "Not found"})
    assert "Not found" in str(exc.value)


def test_check_passes_through_ok_dict(anon_settings):
    data = {"id": "100", "httpCode": 200}
    assert HabrClient(anon_settings)._check(data) is data


@respx.mock
async def test_write_without_creds_raises(anon_settings):
    # No HTTP route registered: the creds check must fire before any request.
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.vote_article(100, "up")
    finally:
        await client.aclose()
    assert str(exc.value) == MISSING_CREDS_MESSAGE


@respx.mock
async def test_vote_article_hits_up_route_with_auth_headers(auth_settings):
    route = respx.post(f"{BASE_URL}articles/100/votes/up/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        await client.vote_article(100, "up")
    finally:
        await client.aclose()

    request = route.calls.last.request
    assert request.url.path == "/kek/v2/articles/100/votes/up/"
    assert request.headers["csrf-token"] == "CSRF456"
    cookie = request.headers["Cookie"]
    assert "connect.sid=SID123" in cookie
    assert "csrf_token=CSRF456" in cookie


@respx.mock
async def test_post_comment_wraps_plain_text(auth_settings):
    route = respx.post(f"{BASE_URL}articles/100/comments/add/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        await client.post_comment(100, "hello & goodbye", parent_id=0)
    finally:
        await client.aclose()

    import json as json_module

    body = json_module.loads(route.calls.last.request.content)
    # Plain text (no tags) gets escaped and wrapped in <p>...</p>.
    assert body["text"] == "<p>hello &amp; goodbye</p>"
    assert body["parent_id"] == 0


@respx.mock
async def test_post_comment_keeps_existing_html(auth_settings):
    route = respx.post(f"{BASE_URL}articles/100/comments/add/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        await client.post_comment(100, "<p>already html</p>", parent_id=5)
    finally:
        await client.aclose()

    import json as json_module

    body = json_module.loads(route.calls.last.request.content)
    # Text that already contains a tag is sent as-is (not double-wrapped).
    assert body["text"] == "<p>already html</p>"
    assert body["parent_id"] == 5


@respx.mock
async def test_bookmark_remove_uses_delete(auth_settings):
    route = respx.delete(f"{BASE_URL}articles/100/bookmarks/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        await client.bookmark_article(100, add=False)
    finally:
        await client.aclose()
    assert route.called


@respx.mock
async def test_non_json_body_raises(anon_settings):
    respx.get(f"{BASE_URL}articles/100/").mock(
        return_value=httpx.Response(404, text="Cannot GET /articles/100/")
    )
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.get_article(100)
    finally:
        await client.aclose()
    assert "не-JSON" in str(exc.value)


@respx.mock
async def test_transport_error_raises(anon_settings):
    respx.get(f"{BASE_URL}articles/100/").mock(
        side_effect=httpx.ConnectError("boom")
    )
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.get_article(100)
    finally:
        await client.aclose()
    assert "Сетевая ошибка" in str(exc.value)


@respx.mock
async def test_vote_article_invalid_direction_raises_no_request(auth_settings):
    # Defense-in-depth: a bad direction must be rejected before any HTTP call.
    route = respx.post(url__regex=r".*").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.vote_article(100, "sideways")
    finally:
        await client.aclose()
    assert "up" in str(exc.value) and "down" in str(exc.value)
    assert not route.called


@respx.mock
async def test_vote_comment_invalid_direction_raises_no_request(auth_settings):
    route = respx.post(url__regex=r".*").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(auth_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.vote_comment(5, "")
    finally:
        await client.aclose()
    assert "up" in str(exc.value) and "down" in str(exc.value)
    assert not route.called


@respx.mock
async def test_empty_2xx_body_is_success(auth_settings):
    # A successful write may return 204/empty body; that must not raise.
    respx.post(f"{BASE_URL}articles/100/votes/up/").mock(
        return_value=httpx.Response(204)
    )
    client = HabrClient(auth_settings)
    try:
        result = await client.vote_article(100, "up")
    finally:
        await client.aclose()
    assert result == {}
