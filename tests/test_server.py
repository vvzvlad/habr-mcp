"""Tests for the multi-tenant FastMCP server.

Two layers:
  * Pure auth helpers (``extract_bearer``, ``resolve``, ``anon_message``) tested
    in isolation with a tiny fake ``Context``.
  * End-to-end tool calls through FastMCP's ``call_tool``: the bearer token is
    injected by patching ``token_from_ctx`` and the store is pre-seeded on disk,
    so the real tool -> registry -> client -> httpx path runs (httpx mocked).
"""

from __future__ import annotations

import httpx
import pytest
import respx

import src.server as server_mod
from src.registry import ClientRegistry
from src.server import (
    ANON,
    NEEDS_LOGIN,
    READY,
    anon_message,
    build_server,
    extract_bearer,
    resolve,
    token_from_ctx,
)
from src.settings import Settings
from src.store import CredStore

BASE_URL = "https://habr.com/kek/v2/"

# A full Cookie header (carries habr_uuid) used to seed a READY identity.
SEED_COOKIE = "connect_sid=s%3Aabc; habr_uuid=uuid-1; hsec_id=def"
SEED_TOKEN = "hmcp_seeded_token"


def _text(result) -> str:
    """Extract the text from a FastMCP call_tool result across SDK versions."""
    content = result[0] if isinstance(result, tuple) else result
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


class _FakeRequestContext:
    def __init__(self, request) -> None:
        self.request = request


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


class _FakeCtx:
    """Minimal stand-in for FastMCP's Context exposing request.headers."""

    def __init__(self, headers: dict[str, str] | None) -> None:
        request = _FakeRequest(headers) if headers is not None else None
        self.request_context = _FakeRequestContext(request)


@pytest.fixture
def seeded_server(tmp_path, monkeypatch):
    """A built server whose store has SEED_TOKEN -> creds, forced READY.

    Patches ``token_from_ctx`` to always return SEED_TOKEN, so every tool call
    resolves to the seeded identity without a real HTTP request.
    """
    CredStore(str(tmp_path)).set(SEED_TOKEN, SEED_COOKIE, "CSRF456", "uuid-1")
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: SEED_TOKEN)
    return build_server(Settings(state_dir=str(tmp_path)))


@pytest.fixture
def seeded_social_server(tmp_path, monkeypatch):
    """Seeded READY server with the social-tools feature toggle enabled."""
    CredStore(str(tmp_path)).set(SEED_TOKEN, SEED_COOKIE, "CSRF456", "uuid-1")
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: SEED_TOKEN)
    return build_server(Settings(state_dir=str(tmp_path), enable_social_tools=True))


@pytest.fixture
def anon_server(tmp_path):
    """A built server with an empty store and no token (every call is ANON)."""
    return build_server(Settings(state_dir=str(tmp_path)))


# -- pure auth helpers -------------------------------------------------------


def test_extract_bearer_missing():
    assert extract_bearer(None) is None
    assert extract_bearer({}) is None


def test_extract_bearer_blank():
    assert extract_bearer({"authorization": ""}) is None
    assert extract_bearer({"authorization": "Bearer "}) is None
    assert extract_bearer({"authorization": "   "}) is None


def test_extract_bearer_strips_scheme_case_insensitive():
    assert extract_bearer({"authorization": "Bearer hmcp_abc"}) == "hmcp_abc"
    # Header name case and scheme case both ignored.
    assert extract_bearer({"Authorization": "bearer hmcp_xyz"}) == "hmcp_xyz"
    # A raw token without the scheme is accepted as-is.
    assert extract_bearer({"authorization": "rawtoken"}) == "rawtoken"


def test_token_from_ctx_reads_header():
    ctx = _FakeCtx({"authorization": "Bearer hmcp_tok"})
    assert token_from_ctx(ctx) == "hmcp_tok"


def test_token_from_ctx_no_request():
    assert token_from_ctx(_FakeCtx(None)) is None
    assert token_from_ctx(None) is None


async def test_resolve_anon_without_token(tmp_path):
    store = CredStore(str(tmp_path))
    registry = ClientRegistry(Settings(state_dir=str(tmp_path)), store)
    state, token, client = await resolve(_FakeCtx(None), store, registry)
    assert state == ANON
    assert token is None
    assert client is None


async def test_resolve_needs_login_with_token_empty_store(tmp_path):
    store = CredStore(str(tmp_path))
    registry = ClientRegistry(Settings(state_dir=str(tmp_path)), store)
    ctx = _FakeCtx({"authorization": "Bearer hmcp_unknown"})
    state, token, client = await resolve(ctx, store, registry)
    assert state == NEEDS_LOGIN
    assert token == "hmcp_unknown"
    assert client is None


async def test_resolve_ready_after_store_set(tmp_path):
    store = CredStore(str(tmp_path))
    store.set("hmcp_known", SEED_COOKIE, "CSRF", "uuid-1")
    registry = ClientRegistry(Settings(state_dir=str(tmp_path)), store)
    ctx = _FakeCtx({"authorization": "Bearer hmcp_known"})
    state, token, client = await resolve(ctx, store, registry)
    assert state == READY
    assert token == "hmcp_known"
    assert client is not None


def test_anon_message_contains_fresh_key_each_call():
    a = anon_message()
    b = anon_message()
    assert "Authorization: Bearer hmcp_" in a
    assert "Authorization: Bearer hmcp_" in b
    # A fresh key is generated on every call.
    assert a != b


# -- tool registration -------------------------------------------------------


async def test_tools_are_registered(anon_server):
    tools = await anon_server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "get_article",
        "create_draft_from_docmost",
        "create_draft_from_gdoc",
        "get_draft",
        "list_drafts",
        "update_draft_from_docmost",
        "update_draft_from_gdoc",
        "delete_draft",
        "resolve_hubs",
        "search_hubs",
        "list_flows",
        "habr_login",
        "auth_status",
    }


async def test_ctx_not_exposed_in_tool_schema(anon_server):
    # FastMCP injects ctx; it must not appear in any tool's input schema.
    tools = await anon_server.list_tools()
    for tool in tools:
        props = (tool.inputSchema or {}).get("properties", {})
        assert "ctx" not in props


# -- gating: ANON / NEEDS_LOGIN ----------------------------------------------


async def test_anon_tool_returns_paste_key_guidance(anon_server):
    out = _text(await anon_server.call_tool("get_article", {"article_id": 1}))
    assert "Authorization: Bearer hmcp_" in out
    assert "habr_login" in out


async def test_anon_habr_login_returns_paste_key_guidance(anon_server):
    out = _text(await anon_server.call_tool("habr_login", {"cookie": "x"}))
    assert "Authorization: Bearer hmcp_" in out


async def test_anon_auth_status(anon_server):
    out = _text(await anon_server.call_tool("auth_status", {}))
    assert "Ключ не задан" in out


async def test_needs_login_tool_message(tmp_path, monkeypatch):
    # Valid token but empty store -> NEEDS_LOGIN guard message.
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: "hmcp_nostore")
    server = build_server(Settings(state_dir=str(tmp_path)))
    out = _text(await server.call_tool("get_draft", {"post_id": 1}))
    assert "habr_login" in out
    assert "DevTools" in out


async def test_needs_login_auth_status(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: "hmcp_nostore")
    server = build_server(Settings(state_dir=str(tmp_path)))
    out = _text(await server.call_tool("auth_status", {}))
    assert "нет логина" in out or "не сохранён" in out


# -- habr_login flow ---------------------------------------------------------


@respx.mock
async def test_habr_login_stores_creds_and_autofetches_csrf(tmp_path, monkeypatch):
    # The csrf is scraped from the feed page when not passed explicitly.
    respx.get("https://habr.com/ru/feed/").mock(
        return_value=httpx.Response(
            200, text='<meta name="csrf-token" content="SCRAPED_CSRF">'
        )
    )
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: "hmcp_login_tok")
    state_dir = str(tmp_path)
    server = build_server(Settings(state_dir=state_dir))
    out = _text(
        await server.call_tool(
            "habr_login", {"cookie": "habr_uuid=u-9; connect_sid=s"}
        )
    )
    assert "Куки сохранены" in out
    # The credentials (with scraped csrf and derived uuid) are persisted.
    creds = CredStore(state_dir).get("hmcp_login_tok")
    assert creds["csrf"] == "SCRAPED_CSRF"
    assert creds["uuid"] == "u-9"


@respx.mock
async def test_habr_login_no_csrf_returns_error(tmp_path, monkeypatch):
    # Feed page without a csrf meta -> auto-detect fails -> ask for csrf_token.
    respx.get("https://habr.com/ru/feed/").mock(
        return_value=httpx.Response(200, text="<html>no token here</html>")
    )
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: "hmcp_nocsrf")
    server = build_server(Settings(state_dir=str(tmp_path)))
    out = _text(await server.call_tool("habr_login", {"cookie": "habr_uuid=u"}))
    assert "csrf_token" in out
    assert CredStore(str(tmp_path)).get("hmcp_nocsrf") is None


async def test_auth_status_ready_hides_token(seeded_server):
    import hashlib

    out = _text(await seeded_server.call_tool("auth_status", {}))
    assert "Готово" in out
    # No substring of the secret token may appear in the output, only a
    # non-reversible sha256 fingerprint prefix.
    assert SEED_TOKEN not in out
    assert SEED_TOKEN[-4:] not in out
    fingerprint = hashlib.sha256(SEED_TOKEN.encode()).hexdigest()[:8]
    assert fingerprint in out


# -- READY end-to-end tool calls ---------------------------------------------


@respx.mock
async def test_list_articles_tool_returns_formatted(seeded_social_server, feed_payload):
    respx.get(f"{BASE_URL}articles/").mock(
        return_value=httpx.Response(200, json=feed_payload)
    )
    out = _text(
        await seeded_social_server.call_tool(
            "list_articles", {"feed": "top", "period": "daily"}
        )
    )
    assert "Вторая статья" in out
    assert "Первая" in out
    assert "id=200" in out


@respx.mock
async def test_get_article_tool(seeded_server, article_payload):
    respx.get(f"{BASE_URL}articles/100/").mock(
        return_value=httpx.Response(200, json=article_payload)
    )
    out = _text(await seeded_server.call_tool("get_article", {"article_id": 100}))
    assert "# Заголовок статьи" in out
    assert "## Раздел" in out


async def test_list_articles_tool_rejects_bad_feed(seeded_social_server):
    out = _text(await seeded_social_server.call_tool("list_articles", {"feed": "bogus"}))
    assert "Недопустимый feed" in out
    assert "top" in out


async def test_vote_article_tool_rejects_bad_direction(seeded_social_server):
    out = _text(
        await seeded_social_server.call_tool(
            "vote_article", {"article_id": 100, "direction": "sideways"}
        )
    )
    assert "Недопустимый direction" in out


@respx.mock
async def test_vote_article_tool_with_creds(seeded_social_server):
    respx.post(f"{BASE_URL}articles/100/votes/up/").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    out = _text(
        await seeded_social_server.call_tool(
            "vote_article", {"article_id": 100, "direction": "up"}
        )
    )
    assert "Голос за статью учтён" in out


async def test_vote_comment_tool_rejects_bad_direction(seeded_social_server):
    out = _text(
        await seeded_social_server.call_tool(
            "vote_comment",
            {"article_id": 100, "comment_id": 5, "direction": "sideways"},
        )
    )
    assert "Недопустимый direction" in out


@respx.mock
async def test_vote_comment_tool_with_creds(seeded_social_server):
    route = respx.post(f"{BASE_URL}articles/100/comments/5/votes").mock(
        return_value=httpx.Response(200, json={"vote": {"value": 1}, "score": 0})
    )
    out = _text(
        await seeded_social_server.call_tool(
            "vote_comment",
            {"article_id": 100, "comment_id": 5, "direction": "up"},
        )
    )
    assert "Голос за комментарий учтён" in out
    assert route.calls.last.request.url.path == "/kek/v2/articles/100/comments/5/votes"


@respx.mock
async def test_resolve_hubs_tool_maps_aliases(seeded_server):
    catalog = {
        "collective": [{"id": "23108", "alias": "smol", "title": "$mol *"}],
        "offtopic": [{"id": "19259", "alias": "closet", "title": "Closet"}],
        "corporative": [],
        "byPost": [{"id": "161", "alias": "habr", "title": "Habr"}],
    }
    respx.get(f"{BASE_URL}publication/suggest-hubs").mock(
        return_value=httpx.Response(200, json=catalog)
    )
    out = _text(
        await seeded_server.call_tool(
            "resolve_hubs", {"aliases": ["habr", "smol", "ghost"]}
        )
    )
    assert "habr → 161 (Habr)" in out
    assert "smol → 23108 ($mol *)" in out
    assert "ghost → не найден" in out


@respx.mock
async def test_search_hubs_tool_filters_by_query(seeded_server):
    catalog = {
        "collective": [
            {"id": 359, "alias": "programming", "title": "Программирование"}
        ],
        "offtopic": [{"id": 21976, "alias": "diy", "title": "DIY или Сделай сам"}],
        "corporative": [],
        "byPost": [],
    }
    respx.get(f"{BASE_URL}publication/suggest-hubs").mock(
        return_value=httpx.Response(200, json=catalog)
    )
    # Non-empty query keeps only matching hubs.
    out = _text(await seeded_server.call_tool("search_hubs", {"query": "diy"}))
    assert "21976" in out
    assert "diy" in out
    assert "programming" not in out
    # Empty query lists the whole catalog.
    out_all = _text(await seeded_server.call_tool("search_hubs", {"query": ""}))
    assert "programming" in out_all
    assert "diy" in out_all


@respx.mock
async def test_create_draft_tool_reports_id(seeded_server, docmost_doc):
    import json as json_module

    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "777", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": json_module.dumps(docmost_doc),
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=777" in out


@respx.mock
async def test_create_draft_from_gdoc_tool_reports_id(seeded_server):
    import json as json_module

    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "888", "ok": True})
    )
    gdoc = {
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {"textRun": {"content": "Тело статьи из Google Docs.\n",
                                         "textStyle": {}}}
                        ]
                    }
                }
            ]
        }
    }
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_gdoc",
            {
                "title": "T",
                "doc": json_module.dumps(gdoc),
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=888" in out


async def test_create_draft_from_gdoc_tool_rejects_bad_json(seeded_server):
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_gdoc", {"title": "T", "doc": "{not json"}
        )
    )
    assert "Не удалось разобрать doc" in out


@respx.mock
async def test_update_draft_from_gdoc_tool_saves(seeded_server, post_data_payload):
    import json as json_module

    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    gdoc = {"body": {"content": [{"paragraph": {"elements": [
        {"textRun": {"content": "new body\n", "textStyle": {}}}]}}]}}
    out = _text(
        await seeded_server.call_tool(
            "update_draft_from_gdoc",
            {
                "post_id": 42,
                "doc": json_module.dumps(gdoc),
                "announce": "Анонс статьи " * 10,
            },
        )
    )
    assert "Черновик 42 сохранён" in out


def test_draft_id_reads_post_key():
    from src.server import _draft_id

    assert _draft_id({"post": "123", "ok": True}) == "123"
    assert _draft_id({"id": "9"}) == "9"
    assert _draft_id({"data": {"id": "5"}}) == "5"
    assert _draft_id({"ok": True}) == "?"


async def test_create_draft_tool_rejects_bad_json(seeded_server):
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost", {"title": "T", "doc": "{not json"}
        )
    )
    assert "Не удалось разобрать doc" in out


# -- lifespan ----------------------------------------------------------------


async def test_lifespan_closes_clients(seeded_server, monkeypatch):
    # The lifespan must close every per-user client created during the session.
    from src.client import HabrClient

    closed = {"count": 0}
    real_aclose = HabrClient.aclose

    async def spy_aclose(self):
        closed["count"] += 1
        await real_aclose(self)

    monkeypatch.setattr(HabrClient, "aclose", spy_aclose)

    # Materialize a per-user client via a tool call so there is something to close.
    with respx.mock:
        respx.get(f"{BASE_URL}articles/100/").mock(
            return_value=httpx.Response(
                200, json={"id": "100", "titleHtml": "x", "textHtml": "<p>x</p>"}
            )
        )
        await seeded_server.call_tool("get_article", {"article_id": 100})

    lifespan = seeded_server.settings.lifespan
    assert lifespan is not None
    async with lifespan(seeded_server):
        assert closed["count"] == 0  # not closed while running
    assert closed["count"] >= 1  # the per-user client was closed on teardown


# -- social-tools feature toggle / list_drafts -------------------------------


async def test_social_tools_enabled_when_toggle_on(seeded_social_server):
    names = {t.name for t in await seeded_social_server.list_tools()}
    assert {
        "search_articles", "list_articles", "get_comments",
        "post_comment", "vote_article", "vote_comment",
    } <= names


@respx.mock
async def test_list_drafts_tool(seeded_server, drafts_payload):
    respx.get(f"{BASE_URL}me").mock(
        return_value=httpx.Response(200, json={"id": "5818348", "alias": "sangman1987"})
    )
    respx.get(f"{BASE_URL}articles/drafts").mock(
        return_value=httpx.Response(200, json=drafts_payload)
    )
    out = _text(await seeded_server.call_tool("list_drafts", {}))
    assert "id=1052760" in out
    assert "Разработка WB-MGE" in out
    assert "поток industrial_engineering" in out
