"""Tests for HabrClient: request shapes, error handling, and auth headers."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.client import (
    AUTHOR_MISSING_CREDS_MESSAGE,
    MISSING_CREDS_MESSAGE,
    HabrApiError,
    HabrClient,
    _cookie_interface_lang,
    _decode_data_uri,
    _upload_filename,
    _validate_announce_length,
    fetch_csrf_token,
    resource_link_uri,
)
from src.converter import serialize_source
from src.settings import Settings

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
            await client.vote_comment(100, 5, "")
    finally:
        await client.aclose()
    assert "up" in str(exc.value) and "down" in str(exc.value)
    assert not route.called


@respx.mock
async def test_vote_comment_up_hits_votes_route_with_value_body(auth_settings):
    import json as json_module

    route = respx.post(f"{BASE_URL}articles/100/comments/5/votes").mock(
        return_value=httpx.Response(200, json={"vote": {"value": 1}, "score": 0})
    )
    client = HabrClient(auth_settings)
    try:
        await client.vote_comment(100, 5, "up")
    finally:
        await client.aclose()

    request = route.calls.last.request
    assert request.url.path == "/kek/v2/articles/100/comments/5/votes"
    body = json_module.loads(request.content)
    assert body == {"value": 1}
    assert request.headers["csrf-token"] == "CSRF456"
    cookie = request.headers["Cookie"]
    assert "connect.sid=SID123" in cookie
    assert "csrf_token=CSRF456" in cookie


@respx.mock
async def test_vote_comment_down_sends_negative_value(auth_settings):
    import json as json_module

    route = respx.post(f"{BASE_URL}articles/100/comments/5/votes").mock(
        return_value=httpx.Response(200, json={"vote": {"value": -1}, "score": 0})
    )
    client = HabrClient(auth_settings)
    try:
        await client.vote_comment(100, 5, "down")
    finally:
        await client.aclose()

    body = json_module.loads(route.calls.last.request.content)
    assert body == {"value": -1}


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


# -- author layer: drafts ----------------------------------------------------


def test_author_headers_without_creds_raises(anon_settings):
    client = HabrClient(anon_settings)
    with pytest.raises(HabrApiError) as exc:
        client._author_headers()
    assert str(exc.value) == AUTHOR_MISSING_CREDS_MESSAGE


def test_author_headers_have_full_bundle(author_settings):
    client = HabrClient(author_settings)
    headers = client._author_headers(referer="https://habr.com/ru/articles/new/")
    assert headers["csrf-token"] == "CSRF456"
    assert headers["habr-user-uuid"] == "uuid-1"
    assert headers["x-app-version"] == "2.329.0"
    assert headers["accept"] == "application/json, text/plain, */*"
    assert headers["Cookie"].startswith("connect_sid=")
    assert headers["referer"] == "https://habr.com/ru/articles/new/"


@respx.mock
async def test_create_draft_hits_save_no_id(author_settings, docmost_doc):
    import json as json_module

    route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "555", "ok": True})
    )
    # A distinctive teaser >= 100 chars; it must land in the preview verbatim and
    # the body text ("Привет, Хабр.") must NOT leak into the preview.
    announce = "Это рукописный анонс до ката для проверки. " + "тизер " * 20
    client = HabrClient(author_settings)
    try:
        result = await client.create_draft(
            "Заголовок",
            docmost_doc,
            hubs=[19791, 4992],
            tags=["t1"],
            flow=2,
            announce=announce,
        )
    finally:
        await client.aclose()

    request = route.calls.last.request
    assert request.url.path == "/kek/v2/publication/save"
    body = json_module.loads(request.content)
    assert body["status"] == "drafted"
    assert body["idempotenceKey"]
    assert body["hubs"] == ["19791", "4992"]
    assert all(isinstance(h, str) for h in body["hubs"])
    assert body["text"]["editorVersion"] == 2
    # flow is required now: a non-empty string.
    assert body["flow"] == "2"
    assert isinstance(body["flow"], str) and body["flow"]
    # text.source is a non-empty JSON string carrying the converted paragraph and
    # uses the canonical "listitem" naming (never "list_item").
    source = body["text"]["source"]
    assert isinstance(source, str) and source
    assert "Привет, Хабр." in source
    assert "list_item" not in source
    # preview (announce) rendered text is EXACTLY the caller's announce (stripped),
    # never derived from / mixed with the article body.
    preview_source = json_module.loads(body["preview"]["source"])
    preview_render = preview_source["content"][0]["content"][0]["text"]
    assert preview_render == announce.strip()
    assert "Привет, Хабр." not in preview_render
    # Author headers must be present.
    assert request.headers["csrf-token"] == "CSRF456"
    assert request.headers["habr-user-uuid"] == "uuid-1"
    assert request.headers["x-app-version"] == "2.329.0"
    assert result["warnings"] == []
    assert result["response"] == {"post": "555", "ok": True}


@respx.mock
async def test_get_draft_hits_post_data(author_settings, post_data_payload):
    route = respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    client = HabrClient(author_settings)
    try:
        data = await client.get_draft(42)
    finally:
        await client.aclose()
    assert route.calls.last.request.url.path == "/kek/v2/publication/post-data/42"
    assert data["postForm"]["id"] == "1047360"


@respx.mock
async def test_list_drafts_resolves_alias_and_hits_drafts(author_settings, drafts_payload):
    # me resolves the author alias; the drafts list then goes to articles/drafts
    # (NO trailing slash) with user/draftType/page/perPage in the query.
    respx.get(f"{BASE_URL}me").mock(
        return_value=httpx.Response(200, json={"id": "5818348", "alias": "sangman1987"})
    )
    route = respx.get(f"{BASE_URL}articles/drafts").mock(
        return_value=httpx.Response(200, json=drafts_payload)
    )
    client = HabrClient(author_settings)
    try:
        result = await client.list_drafts(page=1)
    finally:
        await client.aclose()

    assert result == drafts_payload
    request = route.calls.last.request
    # No trailing slash: articles/drafts/ is a different route that 404s.
    assert request.url.path == "/kek/v2/articles/drafts"
    params = request.url.params
    assert params["user"] == "sangman1987"
    assert params["draftType"] == "posts"
    assert params["page"] == "1"
    assert params["perPage"] == "20"


@respx.mock
async def test_list_drafts_raises_without_alias(author_settings):
    # When `me` carries no alias, list_drafts must fail before hitting drafts.
    respx.get(f"{BASE_URL}me").mock(
        return_value=httpx.Response(200, json={"id": "5818348"})
    )
    drafts_route = respx.get(f"{BASE_URL}articles/drafts").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.list_drafts()
    finally:
        await client.aclose()
    assert "логин" in str(exc.value)
    assert not drafts_route.called


@respx.mock
async def test_update_draft_coerces_types(author_settings, post_data_payload):
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        await client.update_draft(42, title="Новый")
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    assert body["title"] == "Новый"
    # hubs came back as ints from post-data; must be coerced to strings.
    assert body["hubs"] == ["19791", "4992"]
    # editorVersion came back as "2" string; must be coerced to int 2.
    assert body["text"]["editorVersion"] == 2
    assert body["preview"]["editorVersion"] == 2


@respx.mock
async def test_update_draft_sends_fresh_idempotence_key(
    author_settings, post_data_payload
):
    import json as json_module

    # post-data does NOT carry an idempotenceKey; save/<id> rejects a repeated
    # save with the same/absent key (REQUEST_ALREADY_PROCESSED). update_draft must
    # set a fresh key per save, and successive saves must use DIFFERENT keys.
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(author_settings)
    try:
        await client.update_draft(42, title="Первый")
        first = json_module.loads(save_route.calls[0].request.content)
        await client.update_draft(42, title="Второй")
        second = json_module.loads(save_route.calls[1].request.content)
    finally:
        await client.aclose()

    # Each save carries a non-empty string idempotenceKey...
    assert isinstance(first["idempotenceKey"], str) and first["idempotenceKey"]
    assert isinstance(second["idempotenceKey"], str) and second["idempotenceKey"]
    # ...and the key is regenerated per save (never reused).
    assert first["idempotenceKey"] != second["idempotenceKey"]


@respx.mock
async def test_update_draft_refuses_published_post_no_save(
    author_settings, post_data_payload
):
    # A published post must be rejected before any save request goes out, so a
    # stray update never overwrites the live article.
    published = {"postForm": {**post_data_payload["postForm"], "status": "published"}}
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=published)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.update_draft(42, title="Новый")
    finally:
        await client.aclose()
    message = str(exc.value)
    assert "published" in message
    assert "не черновик" in message
    assert "опубликованную статью" in message
    # The save endpoint was never called.
    assert not save_route.called


@respx.mock
async def test_update_draft_allows_drafted_status_and_saves(
    author_settings, post_data_payload
):
    # The default fixture status is "drafted"; the save must proceed normally.
    assert post_data_payload["postForm"]["status"] == "drafted"
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        await client.update_draft(42, title="Новый")
    finally:
        await client.aclose()
    assert save_route.called


@respx.mock
async def test_delete_draft_uses_delete(author_settings):
    route = respx.delete(f"{BASE_URL}articles/drafts/42/posts").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(author_settings)
    try:
        await client.delete_draft(42)
    finally:
        await client.aclose()
    assert route.called
    assert route.calls.last.request.method == "DELETE"


@respx.mock
async def test_suggest_hubs_sends_params(author_settings):
    route = respx.get(f"{BASE_URL}publication/suggest-hubs").mock(
        return_value=httpx.Response(200, json={"collective": []})
    )
    client = HabrClient(author_settings)
    try:
        await client.suggest_hubs(post_id=99)
    finally:
        await client.aclose()
    params = route.calls.last.request.url.params
    assert params["publicationType"] == "topic"
    assert params["postType"] == "simple"
    assert params["postContext"] == "topic"
    assert params["post"] == "99"


@respx.mock
async def test_list_flows_sends_publication_id(author_settings):
    route = respx.get(f"{BASE_URL}refs/flows/wysiwyg").mock(
        return_value=httpx.Response(200, json={"flows": []})
    )
    client = HabrClient(author_settings)
    try:
        await client.list_flows(publication_id=7)
    finally:
        await client.aclose()
    params = route.calls.last.request.url.params
    assert params["publicationId"] == "7"


@respx.mock
async def test_create_draft_no_images_no_extra_http(author_settings, docmost_doc):
    # An image-free doc must not trigger any download/upload HTTP at all.
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(docmost_doc)
        await client.create_draft(
            "t", docmost_doc, hubs=["1"], tags=["t1"], flow="2", announce="А" * 120
        )
    finally:
        await client.aclose()
    assert mapping == {}
    assert warnings == []
    assert not upload_route.called
    assert save_route.called


# -- create_draft required-field validation ----------------------------------


@respx.mock
async def test_create_draft_requires_hubs(author_settings, docmost_doc):
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=None, tags=["t1"], flow="2"
            )
    finally:
        await client.aclose()
    assert "хаб" in str(exc.value)
    assert "resolve_hubs" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_create_draft_requires_tags(author_settings, docmost_doc):
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=["1"], tags=[], flow="2"
            )
    finally:
        await client.aclose()
    assert "тег" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_create_draft_requires_flow(author_settings, docmost_doc):
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        # Whitespace-only flow is treated as missing.
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=["1"], tags=["t1"], flow="   "
            )
    finally:
        await client.aclose()
    assert "поток" in str(exc.value)
    assert "list_flows" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_create_draft_missing_announce_raises(author_settings, docmost_doc):
    # The announce is a separate, required hand-written field — never derived from
    # the body. Omitting it must raise before any save request goes out.
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=["1"], tags=["t1"], flow="2"
            )
    finally:
        await client.aclose()
    assert "announce" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_create_draft_blank_announce_raises(author_settings, docmost_doc):
    # A whitespace-only announce is treated as missing.
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=["1"], tags=["t1"], flow="2", announce="   "
            )
    finally:
        await client.aclose()
    assert "announce" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_create_draft_short_announce_raises(author_settings, docmost_doc):
    # A present-but-too-short announce (< 100 chars) must also raise before save.
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.create_draft(
                "t", docmost_doc, hubs=["1"], tags=["t1"], flow="2", announce="коротко"
            )
    finally:
        await client.aclose()
    assert "100" in str(exc.value)
    assert not save_route.called


@respx.mock
async def test_update_draft_announce_override(author_settings, post_data_payload):
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    announce = "Новый анонс статьи " * 10  # >= 100 chars so validation passes
    client = HabrClient(author_settings)
    try:
        # announce-only update (no docmost_doc) must still rebuild the preview.
        await client.update_draft(42, announce=announce)
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    preview_source = json_module.loads(body["preview"]["source"])
    assert preview_source["content"][0]["content"][0]["text"] == announce.strip()
    assert body["preview"]["editorVersion"] == 2


@respx.mock
async def test_update_draft_body_without_announce_keeps_existing_preview(
    author_settings, post_data_payload, docmost_doc
):
    import json as json_module

    # The announce is a separate field: updating only the body must leave the
    # existing preview from the fetched post-data untouched.
    existing_preview_source = (
        '{"type":"doc","content":[{"type":"paragraph",'
        '"attrs":{"simple":false,"persona":false},'
        '"content":[{"type":"text","text":"СТАРЫЙ АНОНС"}]}]}'
    )
    payload = {
        "postForm": {
            **post_data_payload["postForm"],
            "preview": {
                "source": existing_preview_source,
                "editorVersion": "2",
                "isMarkdown": False,
            },
        }
    }
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        await client.update_draft(42, docmost_doc=docmost_doc)
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    # The body (text) was updated...
    assert "Привет, Хабр." in body["text"]["source"]
    # ...but the preview source is the EXISTING one, unchanged.
    assert json_module.loads(body["preview"]["source"]) == json_module.loads(
        existing_preview_source
    )
    preview_render = json_module.loads(body["preview"]["source"])
    assert preview_render["content"][0]["content"][0]["text"] == "СТАРЫЙ АНОНС"


@respx.mock
async def test_update_draft_short_announce_raises_no_save(
    author_settings, post_data_payload
):
    # A too-short announce on the preview-rebuild path must raise the same clear
    # error as create_draft, before any save request goes out. Only the GET
    # post-data route is mocked; save/<id> must never be called.
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        with pytest.raises(HabrApiError) as exc:
            await client.update_draft(42, announce="слишком коротко")
    finally:
        await client.aclose()
    assert "100" in str(exc.value)
    assert "announce" in str(exc.value)
    assert not save_route.called


# -- author layer: drafts from Google Docs -----------------------------------


@respx.mock
async def test_create_draft_from_gdoc_converts_and_saves(author_settings, gdoc_doc):
    import json as json_module

    route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "900", "ok": True})
    )
    client = HabrClient(author_settings)
    try:
        result = await client.create_draft_from_gdoc(
            "Заголовок",
            gdoc_doc,
            hubs=[1],
            tags=["t1"],
            flow=2,
            announce="А" * 120,
        )
    finally:
        await client.aclose()

    body = json_module.loads(route.calls.last.request.content)
    # The Google Docs body was converted all the way through to a Habr source.
    assert "Привет, Хабр." in body["text"]["source"]
    assert body["status"] == "drafted"
    assert result["response"] == {"post": "900", "ok": True}
    assert result["warnings"] == []


@respx.mock
async def test_create_draft_from_gdoc_merges_conversion_warnings(author_settings):
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "1", "ok": True})
    )
    # A monospace paragraph triggers the code-block conversion warning; that
    # warning must be merged ahead of the (empty) pipeline warnings.
    gdoc = {
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {
                                "textRun": {
                                    "content": "x" * 60 + "\n",
                                    "textStyle": {
                                        "weightedFontFamily": {"fontFamily": "Consolas"}
                                    },
                                }
                            }
                        ]
                    }
                }
            ]
        }
    }
    client = HabrClient(author_settings)
    try:
        result = await client.create_draft_from_gdoc(
            "T", gdoc, hubs=[1], tags=["t1"], flow=2, announce="А" * 120
        )
    finally:
        await client.aclose()
    assert any("code block" in w for w in result["warnings"])


@respx.mock
async def test_update_draft_from_gdoc_converts_body(
    author_settings, post_data_payload, gdoc_doc
):
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        # Pass an explicit announce so the short converted body does not trip the
        # 100-char preview floor (the conversion itself is what we assert here).
        await client.update_draft_from_gdoc(
            42, gdoc_doc=gdoc_doc, announce="Анонс статьи " * 10
        )
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    assert "Привет, Хабр." in body["text"]["source"]


@respx.mock
async def test_update_draft_from_gdoc_without_doc_skips_conversion(
    author_settings, post_data_payload
):
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    client = HabrClient(author_settings)
    try:
        # No gdoc_doc -> the original text source must be preserved untouched.
        await client.update_draft_from_gdoc(42, title="Новый")
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    assert body["title"] == "Новый"
    assert body["text"]["source"] == '{"type":"doc","content":[]}'


# -- resource_link_uri -------------------------------------------------------


def test_resource_link_uri_detects_link():
    assert (
        resource_link_uri({"type": "resource_link", "uri": "https://x/y.json"})
        == "https://x/y.json"
    )


def test_resource_link_uri_rejects_non_link():
    # A ProseMirror doc is a dict but its type is "doc", never a link.
    assert resource_link_uri({"type": "doc", "content": []}) is None
    assert resource_link_uri("https://x/y.png") is None
    assert resource_link_uri({"type": "resource_link", "uri": ""}) is None
    assert resource_link_uri({"type": "resource_link"}) is None


# -- fetch_resource ----------------------------------------------------------

_RES_URL = "https://blobs.example.com/abc.json"


@respx.mock
async def test_fetch_resource_http_ok_no_auth(anon_settings):
    # http(s) returns (bytes, normalized Content-Type) and sends no auth header.
    route = respx.get(_RES_URL).mock(
        return_value=httpx.Response(
            200, content=b"hello", headers={"Content-Type": "image/PNG; charset=x"}
        )
    )
    client = HabrClient(anon_settings)
    try:
        body, content_type = await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()
    assert body == b"hello"
    assert content_type == "image/png"
    # No Authorization header is ever sent.
    assert "authorization" not in {
        k.lower() for k in route.calls.last.request.headers
    }


@respx.mock
async def test_fetch_resource_http_no_content_type_is_none(anon_settings):
    respx.get(_RES_URL).mock(return_value=httpx.Response(200, content=b"x"))
    client = HabrClient(anon_settings)
    try:
        body, content_type = await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()
    assert body == b"x"
    assert content_type is None


@respx.mock
async def test_fetch_resource_http_etag_sha256_match_ok(anon_settings):
    import hashlib

    body = b"sandbox blob"
    digest = hashlib.sha256(body).hexdigest()
    respx.get(_RES_URL).mock(
        return_value=httpx.Response(200, content=body, headers={"ETag": f'"{digest}"'})
    )
    client = HabrClient(anon_settings)
    try:
        got, _ = await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()
    assert got == body


@respx.mock
async def test_fetch_resource_http_etag_sha256_mismatch_raises(anon_settings):
    # A 64-hex ETag that does NOT match the body sha256 means a corrupted blob.
    bad_digest = "0" * 64
    respx.get(_RES_URL).mock(
        return_value=httpx.Response(
            200, content=b"truncated", headers={"ETag": f'W/"{bad_digest}"'}
        )
    )
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError):
            await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_resource_http_opaque_etag_not_verified(anon_settings):
    # An opaque (non-sha256) CDN validator must NOT trigger verification.
    respx.get(_RES_URL).mock(
        return_value=httpx.Response(
            200, content=b"external image", headers={"ETag": '"686897696a7c876b7e"'}
        )
    )
    client = HabrClient(anon_settings)
    try:
        got, _ = await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()
    assert got == b"external image"


async def test_fetch_resource_data_uri_base64_ok(anon_settings):
    import base64

    payload = base64.b64encode(b"\x89PNG-bytes").decode()
    client = HabrClient(anon_settings)
    try:
        body, content_type = await client.fetch_resource(f"data:image/png;base64,{payload}")
    finally:
        await client.aclose()
    assert body == b"\x89PNG-bytes"
    assert content_type == "image/png"


async def test_fetch_resource_data_uri_percent_ok(anon_settings):
    client = HabrClient(anon_settings)
    try:
        body, content_type = await client.fetch_resource("data:text/plain,hello%20world")
    finally:
        await client.aclose()
    assert body == b"hello world"
    assert content_type == "text/plain"


async def test_fetch_resource_malformed_data_uri_raises(anon_settings):
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError):
            # No comma separator -> malformed.
            await client.fetch_resource("data:image/png;base64")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_resource_network_error_raises(anon_settings):
    respx.get(_RES_URL).mock(side_effect=httpx.ConnectError("boom"))
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError):
            await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()


async def test_fetch_resource_malformed_url_raises_habr_error(anon_settings):
    # A malformed URL makes httpx raise InvalidURL (NOT an HTTPError); it must be
    # wrapped as HabrApiError so it never escapes as an unhandled exception.
    client = HabrClient(anon_settings)
    try:
        with pytest.raises(HabrApiError):
            await client.fetch_resource("http://[::1/img")
    finally:
        await client.aclose()


@respx.mock
async def test_fetch_resource_follows_redirect(anon_settings):
    # A 302 (e.g. Google contentUri / CDN) is followed to the final 200 and
    # returns the final body + content type.
    final_url = "https://cdn.example.com/final/pic.png"
    respx.get(_RES_URL).mock(
        return_value=httpx.Response(302, headers={"Location": final_url})
    )
    respx.get(final_url).mock(
        return_value=httpx.Response(
            200, content=b"final-bytes", headers={"Content-Type": "image/png"}
        )
    )
    client = HabrClient(anon_settings)
    try:
        body, content_type = await client.fetch_resource(_RES_URL)
    finally:
        await client.aclose()
    assert body == b"final-bytes"
    assert content_type == "image/png"


# -- image reupload (resolver, no Docmost token) -----------------------------


def _image_doc(src) -> dict:
    """A one-image Docmost doc; ``src`` may be a str URL/data-URI or a dict link."""
    return {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": src}}],
    }


@respx.mock
async def test_reupload_plain_http_url_no_auth(author_settings):
    # A plain http(s) URL src is fetched with NO Authorization header.
    image_url = "https://cdn.example.com/img/abc.png"
    dl_route = respx.get(image_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/x"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(image_url))
    finally:
        await client.aclose()

    assert mapping == {image_url: "https://habrastorage.org/x"}
    assert warnings == []
    assert "authorization" not in {
        k.lower() for k in dl_route.calls.last.request.headers
    }
    # The upload filename is taken from the URL path (abc.png), not derived.
    assert b'filename="abc.png"' in upload_route.calls.last.request.content


@respx.mock
async def test_reupload_resource_link_src_fetched_from_uri(author_settings):
    # A resource_link dict src is fetched from its uri and keyed by that uri.
    uri = "https://blobs.example.com/img/xyz.png"
    src = {"type": "resource_link", "uri": uri, "mimeType": "image/png"}
    respx.get(uri).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/y"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(src))
    finally:
        await client.aclose()

    assert mapping == {uri: "https://habrastorage.org/y"}
    assert warnings == []


@respx.mock
async def test_reupload_data_uri_src_decoded_locally(author_settings):
    # A data: URI src is decoded locally (no network) and uploaded.
    import base64

    payload = base64.b64encode(b"raw-png").decode()
    src = f"data:image/png;base64,{payload}"
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/z"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(src))
    finally:
        await client.aclose()

    assert mapping == {src: "https://habrastorage.org/z"}
    assert warnings == []
    # The bytes posted to habrastorage are the decoded payload, content-type png.
    sent = upload_route.calls.last.request
    assert b"raw-png" in sent.content
    assert b"image/png" in sent.content
    assert b'filename="image.png"' in sent.content


@respx.mock
async def test_reupload_extensionless_url_uses_content_type(author_settings):
    # Sandbox-style blob: extensionless URL but a correct Content-Type header.
    # The upload must carry that content type and a .png filename derived from it.
    image_url = "https://sandbox.example.com/api/sb/3f2a-uuid"
    respx.get(image_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"Content-Type": "image/png"}
        )
    )
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/sb"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(image_url))
    finally:
        await client.aclose()

    assert mapping == {image_url: "https://habrastorage.org/sb"}
    assert warnings == []
    sent = upload_route.calls.last.request
    assert b"image/png" in sent.content
    assert b'filename="image.png"' in sent.content


@respx.mock
async def test_reupload_extensionless_url_octet_stream_falls_back(author_settings):
    # Extensionless URL whose Content-Type is application/octet-stream (unknown
    # image type): filename falls back to image.png and the upload content type
    # is application/octet-stream.
    image_url = "https://sandbox.example.com/api/sb/no-type-uuid"
    respx.get(image_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"Content-Type": "application/octet-stream"}
        )
    )
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/oc"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(image_url))
    finally:
        await client.aclose()

    assert mapping == {image_url: "https://habrastorage.org/oc"}
    assert warnings == []
    sent = upload_route.calls.last.request
    assert b"application/octet-stream" in sent.content
    assert b'filename="image.png"' in sent.content


@respx.mock
async def test_reupload_data_uri_jpeg_uses_jpg_filename(author_settings):
    # data:image/jpeg must upload as image/jpeg with a .jpg filename (not .png).
    import base64

    payload = base64.b64encode(b"jpeg-bytes").decode()
    src = f"data:image/jpeg;base64,{payload}"
    upload_route = respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/j"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(src))
    finally:
        await client.aclose()

    assert mapping == {src: "https://habrastorage.org/j"}
    assert warnings == []
    sent = upload_route.calls.last.request
    assert b"image/jpeg" in sent.content
    assert b'filename="image.jpg"' in sent.content


@respx.mock
async def test_reupload_fetch_failure_skips_with_warning(author_settings):
    # A failing fetch must NOT abort publishing: skip with a warning.
    image_url = "https://cdn.example.com/img/broken.png"
    respx.get(image_url).mock(return_value=httpx.Response(404))
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(image_url))
    finally:
        await client.aclose()

    assert mapping == {}
    assert warnings and "image fetch failed" in warnings[0]


@respx.mock
async def test_reupload_malformed_src_skips_and_valid_image_survives(author_settings):
    # A malformed src raises httpx.InvalidURL (not an HTTPError) inside the GET.
    # It must be skipped with a warning WITHOUT aborting the batch: a second,
    # valid image in the same doc is still fetched and uploaded.
    bad_url = "http://[::1/img"
    good_url = "https://cdn.example.com/img/ok.png"
    doc = {
        "type": "doc",
        "content": [
            {"type": "image", "attrs": {"src": bad_url}},
            {"type": "image", "attrs": {"src": good_url}},
        ],
    }
    respx.get(good_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/ok"})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(doc)
    finally:
        await client.aclose()

    # The valid image survived the bad one (no abort); the bad one was skipped.
    assert mapping == {good_url: "https://habrastorage.org/ok"}
    assert warnings and any("image fetch failed" in w and bad_url in w for w in warnings)


# -- fetch_csrf_token --------------------------------------------------------

_CSRF_PROBE_URL = "https://habr.com/ru/feed/"


@respx.mock
async def test_fetch_csrf_token_simple_meta(anon_settings):
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="TOK_SIMPLE">'
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) == "TOK_SIMPLE"


@respx.mock
async def test_fetch_csrf_token_intermediate_attribute(anon_settings):
    # An intermediate attribute (id=...) between name and content must still match.
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            200,
            text='<meta name="csrf-token" id="x" content="TOK_MID">',
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) == "TOK_MID"


@respx.mock
async def test_fetch_csrf_token_single_quotes(anon_settings):
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            200, text="<meta name='csrf-token' content='TOK_SQ'>"
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) == "TOK_SQ"


@respx.mock
async def test_fetch_csrf_token_content_before_name(anon_settings):
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            200, text='<meta content="TOK_REV" name="csrf-token">'
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) == "TOK_REV"


@respx.mock
async def test_fetch_csrf_token_missing_returns_none(anon_settings):
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(200, text="<html>no token here</html>")
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) is None


@respx.mock
async def test_fetch_csrf_token_network_error_returns_none(anon_settings):
    respx.get(_CSRF_PROBE_URL).mock(side_effect=httpx.ConnectError("boom"))
    assert await fetch_csrf_token("cookie=1", anon_settings) is None


@respx.mock
async def test_fetch_csrf_token_hl_en_hits_en_feed(anon_settings):
    # Regression: an `hl=en` cookie must probe the /en/ feed directly so no
    # language redirect strips the Cookie header and the csrf meta is found.
    en_route = respx.get("https://habr.com/en/feed/").mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="EN_TOK">'
        )
    )
    ru_route = respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(200, text="<html>no token here</html>")
    )
    assert await fetch_csrf_token("hl=en; cookie=1", anon_settings) == "EN_TOK"
    assert en_route.called
    assert not ru_route.called


@respx.mock
async def test_fetch_csrf_token_manual_redirect_preserves_cookie(anon_settings):
    # A 302 must be followed manually with the Cookie re-sent so the target page
    # loads as the logged-in session and carries the csrf meta.
    target = "https://habr.com/ru/feed/articles/"
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(302, headers={"location": target})
    )
    respx.get(target).mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="REDIR_TOK">'
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) == "REDIR_TOK"


@respx.mock
async def test_fetch_csrf_token_cross_host_redirect_not_followed(anon_settings):
    # Security: a redirect to an external host must NOT be followed with the
    # session Cookie re-sent, so an external page's csrf meta is never picked up.
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://evil.example.com/steal"}
        )
    )
    evil_route = respx.get("https://evil.example.com/steal").mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="LEAKED">'
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) is None
    assert evil_route.call_count == 0


@respx.mock
async def test_fetch_csrf_token_subdomain_redirect_not_followed(anon_settings):
    # Security: a redirect to a DIFFERENT host (even a habr.com subdomain) must
    # NOT be followed with the session Cookie re-sent.
    respx.get(_CSRF_PROBE_URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://x.habr.com/steal"}
        )
    )
    sub_route = respx.get("https://x.habr.com/steal").mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="LEAKED">'
        )
    )
    assert await fetch_csrf_token("cookie=1", anon_settings) is None
    assert sub_route.call_count == 0


def test_cookie_interface_lang():
    assert _cookie_interface_lang("hl=en,ru; foo=bar") == "en"
    assert _cookie_interface_lang("foo=bar; baz=1") == "ru"


def test_cookie_interface_lang_tolerates_spaces():
    assert _cookie_interface_lang("hl = en ; x=1") == "en"


def test_cookie_interface_lang_rejects_malformed():
    # A malformed hl value must fall back to the default rather than corrupt the
    # probe URL path; uppercase doesn't match the lowercase 2-letter pattern.
    assert _cookie_interface_lang("hl=en/feed") == "ru"
    assert _cookie_interface_lang("hl=EN") == "ru"
    assert _cookie_interface_lang("hl=en") == "en"


# -- upload_image response shapes --------------------------------------------


@respx.mock
async def test_upload_image_src_key_in_body(author_settings):
    # A body carrying only "src" (no "url") must still be accepted.
    url = "https://habrastorage.org/getpro/habr/src-shape"
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"src": url})
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result == url


@respx.mock
async def test_upload_image_nested_data_url(author_settings):
    # A nested {"data": {"url": ...}} body must be unwrapped.
    url = "https://habrastorage.org/getpro/habr/nested"
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"data": {"url": url}})
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result == url


@respx.mock
async def test_upload_image_regex_fallback_in_json_text(author_settings):
    # A JSON body with no usable url falls back to the habrastorage regex over
    # the raw response text (which still contains the URL).
    # The regex captures \S+ greedily, so the URL must end at a whitespace
    # boundary; JSON serializes the value as "<url>", whose closing quote is the
    # next char. Append a space inside the value so the match stops cleanly.
    url = "https://habrastorage.org/getpro/habr/regex-json"
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"ok": True, "location": f"{url} "})
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result == url


@respx.mock
async def test_upload_image_regex_fallback_non_json_body(author_settings):
    # A non-JSON (HTML) body containing a habrastorage URL is matched by regex.
    # The regex captures \S+ greedily, so the URL must be whitespace-delimited.
    url = "https://habrastorage.org/getpro/habr/regex-html"
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, text=f"uploaded to {url} done")
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result == url


@respx.mock
async def test_upload_image_non_json_no_match_returns_none(author_settings):
    # A non-JSON body with no habrastorage URL yields None (clean failure).
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, text="<html>no link here</html>")
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result is None


@respx.mock
async def test_upload_image_transport_error_returns_none(author_settings):
    # Transport errors are swallowed (publish must not abort): returns None.
    respx.post(f"{BASE_URL}publication/upload").mock(
        side_effect=httpx.ConnectError("boom")
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result is None


@respx.mock
async def test_upload_image_dict_no_url_no_match_returns_none(author_settings):
    # A dict body with no url AND raw text with no habrastorage match -> None.
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(author_settings)
    try:
        result = await client.upload_image(b"img", "image.png", "image/png")
    finally:
        await client.aclose()
    assert result is None


# -- _upload_filename: content-type -> extension -----------------------------


def test_upload_filename_extensionless_url_uses_content_type():
    base = "https://h/api/sb/3f2a-uuid"
    assert _upload_filename(base, "image/gif") == "image.gif"
    assert _upload_filename(base, "image/webp") == "image.webp"
    assert _upload_filename(base, "image/svg+xml") == "image.svg"


def test_upload_filename_data_uri_uses_content_type():
    assert _upload_filename("data:image/png;base64,AAA", "image/png") == "image.png"
    assert _upload_filename("data:image/jpeg;base64,AAA", "image/jpeg") == "image.jpg"


def test_upload_filename_unknown_type_falls_back_to_png():
    # An extensionless URL with an unknown/None content type defaults to image.png.
    assert _upload_filename("https://h/api/sb/uuid", "application/octet-stream") == "image.png"
    assert _upload_filename("https://h/api/sb/uuid", None) == "image.png"


def test_upload_filename_keeps_existing_extension():
    # A URL with a real extension keeps its own filename, regardless of type.
    assert _upload_filename("https://cdn.example.com/img/abc.png", "image/jpeg") == "abc.png"


# -- _decode_data_uri: base64 failure ----------------------------------------


def test_decode_data_uri_invalid_base64_raises():
    # A base64 payload with bad padding raises binascii.Error -> HabrApiError.
    with pytest.raises(HabrApiError):
        _decode_data_uri("data:;base64,A")


# -- _validate_announce_length: upper bound (off-by-one) ----------------------


def test_validate_announce_length_over_max_raises():
    with pytest.raises(HabrApiError) as exc:
        _validate_announce_length("А" * 3001)
    message = str(exc.value)
    assert "слишком длинный" in message
    assert "3000" in message


def test_validate_announce_length_at_max_ok():
    # Exactly 3000 chars is the upper boundary and must NOT raise.
    _validate_announce_length("А" * 3000)


# -- _auth_headers: full cookie+token priority over legacy connect.sid --------


def test_auth_headers_full_cookie_beats_legacy_connect_sid():
    # When both the full habr_cookie+token and the legacy connect_sid are set,
    # the full Cookie header wins verbatim (never the connect.sid construction).
    settings = Settings(
        habr_lang="ru",
        habr_cookie="connect_sid=full; hsec_id=x",
        habr_csrf_token="CSRF",
        habr_connect_sid="LEGACYSID",
        habr_csrf_cookie_name="csrf_token",
        proxy=None,
        per_page=20,
    )
    client = HabrClient(settings)
    headers = client._auth_headers()
    assert headers["Cookie"] == "connect_sid=full; hsec_id=x"
    assert "connect.sid=LEGACYSID" not in headers["Cookie"]
    assert headers["csrf-token"] == "CSRF"


# -- integration: uncovered branches -----------------------------------------


@respx.mock
async def test_reupload_upload_failed_warns(author_settings):
    # The image fetch succeeds but the upload returns no usable url and no regex
    # match: the mapping stays empty and an "image upload failed" warning is added
    # (distinct from the fetch-failure path).
    image_url = "https://cdn.example.com/img/upfail.png"
    respx.get(image_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = HabrClient(author_settings)
    try:
        mapping, warnings = await client._reupload_images(_image_doc(image_url))
    finally:
        await client.aclose()

    assert mapping == {}
    assert warnings and any(
        "image upload failed" in w and image_url in w for w in warnings
    )


@respx.mock
async def test_create_draft_preview_doc_skips_announce_validation(author_settings, docmost_doc):
    # When a preview_doc is supplied the announce is None and validation is
    # skipped: the save succeeds without any announce error.
    import json as json_module

    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "777", "ok": True})
    )
    preview_doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Тизер"}]}
        ],
    }
    client = HabrClient(author_settings)
    try:
        result = await client.create_draft(
            "Заголовок",
            docmost_doc,
            hubs=[19791],
            tags=["t1"],
            flow=2,
            announce=None,
            preview_doc=preview_doc,
        )
    finally:
        await client.aclose()

    assert save_route.called
    body = json_module.loads(save_route.calls.last.request.content)
    # The preview is built from preview_doc, not the (absent) announce.
    assert body["preview"]["source"] == serialize_source(preview_doc)
    assert result["response"] == {"post": "777", "ok": True}


@respx.mock
async def test_update_draft_preview_doc_rebuilds_preview(author_settings, post_data_payload):
    # A supplied preview_doc rebuilds the preview from that doc with editorVersion 2.
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    save_route = respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    preview_doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Новый тизер"}]}
        ],
    }
    client = HabrClient(author_settings)
    try:
        await client.update_draft(42, preview_doc=preview_doc)
    finally:
        await client.aclose()

    body = json_module.loads(save_route.calls.last.request.content)
    assert body["preview"]["source"] == serialize_source(preview_doc)
    assert body["preview"]["editorVersion"] == 2
