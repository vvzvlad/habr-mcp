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
    fetch_csrf_token,
)

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
    announce = "А" * 120  # >= 100 chars so the announce passes validation
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
    # preview (announce) rendered text is at least 100 chars.
    preview_source = json_module.loads(body["preview"]["source"])
    preview_render = preview_source["content"][0]["content"][0]["text"]
    assert len(preview_render) >= 100
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
async def test_create_draft_short_announce_raises(author_settings, docmost_doc):
    # The fixture body ("Привет, Хабр.") renders to < 100 chars, so the derived
    # announce is too short and create must raise before any save.
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
    assert "100 символов" in str(exc.value)
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
    assert "100 символов" in str(exc.value)
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


# -- image reupload auth (Docmost token scoping) -----------------------------


def _gdoc_image_doc(src: str) -> dict:
    """A one-image Docmost doc with an absolute image src (post-conversion shape)."""
    return {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": src}}],
    }


@respx.mock
async def test_reupload_external_image_no_docmost_token(author_settings):
    # A Google contentUri (googleusercontent.com) must be downloaded WITHOUT the
    # Docmost bearer token, even when DOCMOST_API_TOKEN is configured.
    settings = author_settings.model_copy(
        update={
            "docmost_base_url": "https://wiki.example.com",
            "docmost_api_token": "DOCMOST_SECRET",
        }
    )
    google_url = "https://lh3.googleusercontent.com/secret-image"
    dl_route = respx.get(google_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/x"})
    )
    client = HabrClient(settings)
    try:
        mapping, warnings = await client._reupload_images(_gdoc_image_doc(google_url))
    finally:
        await client.aclose()

    assert mapping == {google_url: "https://habrastorage.org/x"}
    assert warnings == []
    # The download request must NOT carry the Docmost Authorization header.
    assert "authorization" not in {
        k.lower() for k in dl_route.calls.last.request.headers
    }


@respx.mock
async def test_reupload_relative_docmost_image_gets_token(author_settings):
    # A relative src (joined with the Docmost base) IS a Docmost-hosted image and
    # must receive the Docmost bearer token.
    settings = author_settings.model_copy(
        update={
            "docmost_base_url": "https://wiki.example.com",
            "docmost_api_token": "DOCMOST_SECRET",
        }
    )
    dl_route = respx.get("https://wiki.example.com/api/files/abc.png").mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/y"})
    )
    client = HabrClient(settings)
    try:
        mapping, _ = await client._reupload_images(
            _gdoc_image_doc("/api/files/abc.png")
        )
    finally:
        await client.aclose()

    assert mapping == {"/api/files/abc.png": "https://habrastorage.org/y"}
    assert (
        dl_route.calls.last.request.headers["authorization"] == "Bearer DOCMOST_SECRET"
    )


@respx.mock
async def test_reupload_absolute_docmost_host_image_gets_token(author_settings):
    # An absolute URL whose host == the Docmost base host is Docmost-hosted, so it
    # must receive the Docmost bearer token.
    settings = author_settings.model_copy(
        update={
            "docmost_base_url": "https://wiki.example.com",
            "docmost_api_token": "DOCMOST_SECRET",
        }
    )
    same_host = "https://wiki.example.com/files/a.png"
    dl_route = respx.get(same_host).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/z"})
    )
    client = HabrClient(settings)
    try:
        await client._reupload_images(_gdoc_image_doc(same_host))
    finally:
        await client.aclose()

    assert (
        dl_route.calls.last.request.headers["authorization"] == "Bearer DOCMOST_SECRET"
    )


@respx.mock
async def test_reupload_host_match_case_insensitive_and_port_agnostic(author_settings):
    # Base host differs from the image URL only in letter case AND an explicit
    # default port. The normalized hostname match must still treat it as Docmost
    # and attach the bearer token (regression: netloc compare was case/port-strict).
    settings = author_settings.model_copy(
        update={
            "docmost_base_url": "https://Wiki.Example.com",
            "docmost_api_token": "DOCMOST_SECRET",
        }
    )
    image_url = "https://wiki.example.com:443/files/a.png"
    dl_route = respx.get(image_url).mock(
        return_value=httpx.Response(
            200, content=b"img", headers={"content-type": "image/png"}
        )
    )
    respx.post(f"{BASE_URL}publication/upload").mock(
        return_value=httpx.Response(200, json={"url": "https://habrastorage.org/q"})
    )
    client = HabrClient(settings)
    try:
        await client._reupload_images(_gdoc_image_doc(image_url))
    finally:
        await client.aclose()

    assert (
        dl_route.calls.last.request.headers["authorization"] == "Bearer DOCMOST_SECRET"
    )


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
