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
    # ANON auth_status must hand back a freshly generated, ready-to-paste key
    # (not a literal "<ключ>" placeholder).
    out = _text(await anon_server.call_tool("auth_status", {}))
    assert "Authorization: Bearer hmcp_" in out


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
    # Feed page without a csrf meta -> auto-detect fails -> report a likely
    # bad/expired cookie (there is no csrf_token parameter to ask for anymore).
    respx.get("https://habr.com/ru/feed/").mock(
        return_value=httpx.Response(200, text="<html>no token here</html>")
    )
    monkeypatch.setattr(server_mod, "token_from_ctx", lambda ctx: "hmcp_nocsrf")
    server = build_server(Settings(state_dir=str(tmp_path)))
    out = _text(await server.call_tool("habr_login", {"cookie": "habr_uuid=u"}))
    assert "csrf" in out.lower()
    assert "cookie" in out.lower()
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
async def test_get_article_tool(seeded_server, article_payload):
    respx.get(f"{BASE_URL}articles/100/").mock(
        return_value=httpx.Response(200, json=article_payload)
    )
    out = _text(await seeded_server.call_tool("get_article", {"article_id": 100}))
    assert "# Заголовок статьи" in out
    assert "## Раздел" in out


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
async def test_create_draft_from_docmost_accepts_dict(seeded_server, docmost_doc):
    # Regression: FastMCP pre-parses an object-shaped JSON string into a dict
    # before validation, so ``doc`` arrives as a dict (not a string) — the tool
    # must accept it instead of rejecting it at the schema layer.
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "999", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": docmost_doc,
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=999" in out


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


@respx.mock
async def test_create_draft_from_gdoc_accepts_dict(seeded_server):
    # Regression: FastMCP pre-parses an object-shaped JSON string into a dict
    # before validation, so ``doc`` arrives as a dict (not a string) — the tool
    # must accept it instead of rejecting it at the schema layer.
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "555", "ok": True})
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
                "doc": gdoc,
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=555" in out


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


@respx.mock
async def test_update_draft_from_docmost_tool_saves(seeded_server, post_data_payload,
                                                    docmost_doc):
    # Regression: ``doc`` arrives as a dict (FastMCP pre-parses an object-shaped
    # JSON string before validation), so the optional ``doc`` must accept it.
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    out = _text(
        await seeded_server.call_tool(
            "update_draft_from_docmost",
            {
                "post_id": 42,
                "doc": docmost_doc,
                "announce": "Анонс статьи " * 10,
            },
        )
    )
    assert "Черновик 42 сохранён" in out


# -- doc as a resource_link --------------------------------------------------

_DOC_LINK_URI = "https://blobs.example.com/page.json"


@respx.mock
async def test_create_draft_from_docmost_accepts_resource_link(seeded_server,
                                                               docmost_doc):
    import json as json_module

    # The doc body arrives as an MCP resource_link; habr fetches its uri itself.
    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(docmost_doc).encode())
    )
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "555", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": {"type": "resource_link", "uri": _DOC_LINK_URI},
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=555" in out


@respx.mock
async def test_create_draft_from_gdoc_accepts_resource_link(seeded_server):
    import json as json_module

    gdoc = {"body": {"content": [{"paragraph": {"elements": [
        {"textRun": {"content": "body\n", "textStyle": {}}}]}}]}}
    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(gdoc).encode())
    )
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "556", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_gdoc",
            {
                "title": "T",
                "doc": {"type": "resource_link", "uri": _DOC_LINK_URI},
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=556" in out


@respx.mock
async def test_update_draft_from_docmost_accepts_resource_link(seeded_server,
                                                               post_data_payload,
                                                               docmost_doc):
    import json as json_module

    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(docmost_doc).encode())
    )
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    out = _text(
        await seeded_server.call_tool(
            "update_draft_from_docmost",
            {
                "post_id": 42,
                "doc": {"type": "resource_link", "uri": _DOC_LINK_URI},
                "announce": "Анонс статьи " * 10,
            },
        )
    )
    assert "Черновик 42 сохранён" in out


@respx.mock
async def test_update_draft_from_gdoc_accepts_resource_link(seeded_server,
                                                            post_data_payload):
    import json as json_module

    gdoc = {"body": {"content": [{"paragraph": {"elements": [
        {"textRun": {"content": "new\n", "textStyle": {}}}]}}]}}
    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(gdoc).encode())
    )
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json=post_data_payload)
    )
    respx.post(f"{BASE_URL}publication/save/42").mock(
        return_value=httpx.Response(200, json={})
    )
    out = _text(
        await seeded_server.call_tool(
            "update_draft_from_gdoc",
            {
                "post_id": 42,
                "doc": {"type": "resource_link", "uri": _DOC_LINK_URI},
                "announce": "Анонс статьи " * 10,
            },
        )
    )
    assert "Черновик 42 сохранён" in out


@respx.mock
async def test_create_draft_from_docmost_resource_link_fetch_error(seeded_server):
    # A failed body-link fetch returns a Russian error; no draft is created.
    respx.get(_DOC_LINK_URI).mock(return_value=httpx.Response(500))
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "x", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": {"type": "resource_link", "uri": _DOC_LINK_URI},
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "Не удалось загрузить ресурс" in out
    assert not save_route.called


@respx.mock
async def test_create_draft_from_docmost_resource_link_malformed_uri(seeded_server):
    # A malformed body-link uri (httpx.InvalidURL, NOT an HTTPError) must surface
    # as a clean Russian error string, not an unhandled exception.
    save_route = respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "x", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": {"type": "resource_link", "uri": "http://[::1/page.json"},
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "Не удалось загрузить ресурс" in out
    assert not save_route.called


# -- _doc_link_uri helper + doc-arg leniency --------------------------------


def test_doc_link_uri_recognizes_link_shapes():
    from src.server import _doc_link_uri

    # 1. canonical resource_link -> its uri
    assert _doc_link_uri(
        {"type": "resource_link", "uri": _DOC_LINK_URI}
    ) == _DOC_LINK_URI
    # 2. any other dict carrying a uri (no type:"doc") -> that uri
    assert _doc_link_uri({"uri": "https://x/y.json"}) == "https://x/y.json"
    # 3a. a bare http(s) URL string -> that string
    assert _doc_link_uri("https://x/y.json") == "https://x/y.json"
    # 3b. a data: URL string -> that string
    assert _doc_link_uri("data:application/json,{}") == "data:application/json,{}"


def test_doc_link_uri_returns_none_for_inline_docs():
    from src.server import _doc_link_uri

    # A serialized ProseMirror doc string is inline (starts with "{").
    assert _doc_link_uri('{"type":"doc","content":[]}') is None
    # A parsed inline doc dict is inline.
    assert _doc_link_uri({"type": "doc", "content": []}) is None
    # A plain non-URL string is inline.
    assert _doc_link_uri("hello") is None
    # Risk-bearing inline shapes with no top-level type/uri stay inline:
    # a ProseMirror fragment, a doc-like object with content, and a
    # Google-Docs-shaped object must NOT be mistaken for a link.
    assert _doc_link_uri({"content": []}) is None
    assert _doc_link_uri({"id": "x", "content": {}}) is None
    assert _doc_link_uri({"title": "Doc", "body": {}}) is None


def test_doc_link_uri_rejects_dirty_or_nonstring_uri():
    from src.server import _doc_link_uri

    # An empty/whitespace/non-string ``uri`` is not a usable link.
    assert _doc_link_uri({"uri": ""}) is None
    assert _doc_link_uri({"uri": "   "}) is None
    assert _doc_link_uri({"uri": 123}) is None


@respx.mock
async def test_create_draft_from_docmost_accepts_bare_url_string(seeded_server,
                                                                 docmost_doc):
    import json as json_module

    # A bare URL string is dereferenced like a resource_link.
    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(docmost_doc).encode())
    )
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "557", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": _DOC_LINK_URI,
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=557" in out


@respx.mock
async def test_create_draft_from_docmost_accepts_uri_object(seeded_server,
                                                            docmost_doc):
    import json as json_module

    # A {"uri": ...} object (no type) is dereferenced like a resource_link.
    respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=json_module.dumps(docmost_doc).encode())
    )
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "558", "ok": True})
    )
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": {"uri": _DOC_LINK_URI},
                "hubs": ["161"],
                "tags": ["t1"],
                "flow": "2",
                "announce": "А" * 120,
            },
        )
    )
    assert "id=558" in out


@respx.mock
async def test_create_draft_from_docmost_json_string_not_fetched(seeded_server,
                                                                 docmost_doc):
    import json as json_module

    # Regression: a doc passed as a JSON STRING stays inline — never fetched.
    link_route = respx.get(_DOC_LINK_URI).mock(
        return_value=httpx.Response(200, content=b"{}")
    )
    respx.post(f"{BASE_URL}publication/save").mock(
        return_value=httpx.Response(200, json={"post": "559", "ok": True})
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
    assert "id=559" in out
    assert not link_route.called


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


async def test_create_draft_tool_rejects_wrong_shape_doc(seeded_server):
    # Valid JSON but not a ProseMirror doc must yield a clean Russian message,
    # not a raw ToolError leaking ValueError("not a ProseMirror document").
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_docmost",
            {
                "title": "T",
                "doc": '{"foo": 1}',
                "hubs": ["1"],
                "tags": ["t"],
                "flow": "2",
                "announce": "a" * 150,
            },
        )
    )
    assert "ProseMirror" in out


async def test_create_draft_from_gdoc_rejects_wrong_shape_doc(seeded_server):
    # A gdoc tool must surface the Google-Docs-flavored Russian message,
    # not the ProseMirror one, on a valid-JSON-but-wrong-shape doc.
    out = _text(
        await seeded_server.call_tool(
            "create_draft_from_gdoc",
            {
                "title": "T",
                "doc": '{"foo": 1}',
                "hubs": ["1"],
                "tags": ["t"],
                "flow": "2",
                "announce": "a" * 150,
            },
        )
    )
    assert "Google Docs" in out


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


# -- list_drafts -------------------------------------------------------------


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


# -- Phase 4: extracted pure helper unit tests -------------------------------


def test_parse_doc_arg_json_object_string():
    # A JSON object string decodes to a dict with no error.
    from src.server import _parse_doc_arg

    parsed, err = _parse_doc_arg('{"type": "doc", "x": 1}')
    assert parsed == {"type": "doc", "x": 1}
    assert err is None


def test_parse_doc_arg_bytes_input():
    # ``bytes`` (as fetched from a resource_link) are decoded like a str.
    from src.server import _parse_doc_arg

    parsed, err = _parse_doc_arg(b'{"k": "v"}')
    assert parsed == {"k": "v"}
    assert err is None


def test_parse_doc_arg_dict_passthrough():
    # An already-dict value is returned as-is (same object), no error.
    from src.server import _parse_doc_arg

    doc = {"type": "doc"}
    parsed, err = _parse_doc_arg(doc)
    assert parsed is doc
    assert err is None


def test_parse_doc_arg_list_passthrough():
    # A list value passes through untouched, no error.
    from src.server import _parse_doc_arg

    doc = [1, 2, 3]
    parsed, err = _parse_doc_arg(doc)
    assert parsed is doc
    assert err is None


def test_parse_doc_arg_broken_json():
    # Broken JSON yields no value and a Russian parse-error message.
    from src.server import _parse_doc_arg

    parsed, err = _parse_doc_arg("{not json")
    assert parsed is None
    assert err is not None
    assert err.startswith("Не удалось разобрать doc")


def test_warnings_suffix_none_and_empty():
    # None and an empty list both render to an empty suffix.
    from src.server import _warnings_suffix

    assert _warnings_suffix(None) == ""
    assert _warnings_suffix([]) == ""


def test_warnings_suffix_bullets():
    # A non-empty list renders a 'Предупреждения:' bullet block.
    from src.server import _warnings_suffix

    assert _warnings_suffix(["a", "b"]) == "\nПредупреждения:\n- a\n- b"


def test_filter_hubs_query_keeps_only_matching():
    # A non-empty query keeps matching hubs and drops the rest.
    from src.server import _filter_hubs

    catalog = {
        "collective": [{"id": 359, "alias": "programming", "title": "Программирование"}],
        "offtopic": [{"id": 21976, "alias": "diy", "title": "DIY или Сделай сам"}],
        "corporative": [],
        "byPost": [],
    }
    out = _filter_hubs(catalog, "diy", 40)
    assert "21976" in out
    assert "diy" in out
    assert "programming" not in out


def test_filter_hubs_empty_query_lists_all():
    # An empty query lists the whole catalog.
    from src.server import _filter_hubs

    catalog = {
        "collective": [{"id": 359, "alias": "programming", "title": "Программирование"}],
        "offtopic": [{"id": 21976, "alias": "diy", "title": "DIY"}],
        "corporative": [],
        "byPost": [],
    }
    out = _filter_hubs(catalog, "", 40)
    assert "programming" in out
    assert "diy" in out


def test_filter_hubs_caps_at_limit_with_summary():
    # When matches exceed limit, output is capped and a summary line is appended.
    from src.server import _filter_hubs

    catalog = {
        "collective": [
            {"id": i, "alias": f"h{i}", "title": f"Hub {i}"} for i in range(5)
        ],
        "offtopic": [],
        "corporative": [],
        "byPost": [],
    }
    out = _filter_hubs(catalog, "", 2)
    lines = out.split("\n")
    # 2 hub lines + 1 trailing summary line.
    assert len(lines) == 3
    assert lines[-1] == "… показано 2 из 5"


def test_filter_hubs_empty_result_with_query():
    # No match for a query -> a query-specific not-found message.
    from src.server import _filter_hubs

    catalog = {"collective": [{"id": 1, "alias": "a", "title": "A"}],
               "offtopic": [], "corporative": [], "byPost": []}
    assert _filter_hubs(catalog, "zzz", 40) == "Хабы по запросу 'zzz' не найдены."


def test_filter_hubs_empty_result_empty_query():
    # An empty catalog with an empty query -> the generic not-found message.
    from src.server import _filter_hubs

    catalog = {"collective": [], "offtopic": [], "corporative": [], "byPost": []}
    assert _filter_hubs(catalog, "", 40) == "Хабы не найдены."


def test_match_hub_aliases_known_and_unknown():
    # Known aliases map to 'alias → id (title)'; unknown -> 'alias → не найден'.
    from src.server import _match_hub_aliases

    catalog = {
        "collective": [{"id": "23108", "alias": "smol", "title": "$mol *"}],
        "offtopic": [],
        "corporative": [],
        "byPost": [{"id": "161", "alias": "habr", "title": "Habr"}],
    }
    out = _match_hub_aliases(catalog, ["habr", "ghost"])
    assert "habr → 161 (Habr)" in out
    assert "ghost → не найден" in out


def test_match_hub_aliases_empty_list():
    # An empty aliases list -> a clear "nothing passed" message.
    from src.server import _match_hub_aliases

    catalog = {"collective": [], "offtopic": [], "corporative": [], "byPost": []}
    assert _match_hub_aliases(catalog, []) == "Не передано ни одного алиаса."


def test_match_hub_aliases_title_falls_back_to_titlehtml():
    # When 'title' is missing, the 'titleHtml' value is used verbatim.
    from src.server import _match_hub_aliases

    catalog = {
        "collective": [{"id": "9", "alias": "x", "titleHtml": "X <b>html</b>"}],
        "offtopic": [], "corporative": [], "byPost": [],
    }
    out = _match_hub_aliases(catalog, ["x"])
    assert out == "x → 9 (X <b>html</b>)"


def test_match_hub_aliases_dedups_across_groups():
    # A duplicate alias across groups keeps the FIRST occurrence (setdefault).
    from src.server import _match_hub_aliases

    catalog = {
        "collective": [{"id": "1", "alias": "dup", "title": "First"}],
        "offtopic": [{"id": "2", "alias": "dup", "title": "Second"}],
        "corporative": [],
        "byPost": [],
    }
    out = _match_hub_aliases(catalog, ["dup"])
    assert out == "dup → 1 (First)"


def test_format_flows_renders_lines():
    # Flows render as 'id  alias  title' lines.
    from src.server import _format_flows

    data = {"flows": [{"id": "2", "alias": "backend", "title": "Бэкенд"}]}
    assert _format_flows(data) == "2  backend  Бэкенд"


def test_format_flows_skips_non_dict_entries():
    # Non-dict entries in the flows list are skipped.
    from src.server import _format_flows

    data = {"flows": [{"id": "2", "alias": "backend", "title": "Бэкенд"}, "junk", 5]}
    assert _format_flows(data) == "2  backend  Бэкенд"


def test_format_flows_empty():
    # Empty/missing flows -> the not-found message.
    from src.server import _format_flows

    assert _format_flows({"flows": []}) == "Потоки не найдены."
    assert _format_flows({}) == "Потоки не найдены."


def test_draft_id_non_dict_input():
    # A non-dict response can never yield an id -> "?".
    from src.server import _draft_id

    assert _draft_id(None) == "?"
    assert _draft_id("x") == "?"
    assert _draft_id([]) == "?"


# -- Phase 1: additional READY end-to-end tool calls -------------------------


@respx.mock
async def test_delete_draft_tool_success(seeded_server):
    # A successful DELETE confirms the deletion in the output.
    respx.delete(f"{BASE_URL}articles/drafts/42/posts").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    out = _text(await seeded_server.call_tool("delete_draft", {"post_id": 42}))
    assert "Черновик 42 удалён" in out


@respx.mock
async def test_delete_draft_tool_error(seeded_server):
    # A Habr error dict surfaces as the error message string.
    respx.delete(f"{BASE_URL}articles/drafts/42/posts").mock(
        return_value=httpx.Response(200, json={"httpCode": 500, "message": "Fail"})
    )
    out = _text(await seeded_server.call_tool("delete_draft", {"post_id": 42}))
    assert out == "Fail"


@respx.mock
async def test_get_draft_tool_error(seeded_server):
    # A Habr error on post-data surfaces as the error message string.
    respx.get(f"{BASE_URL}publication/post-data/42").mock(
        return_value=httpx.Response(200, json={"httpCode": 404, "message": "No draft"})
    )
    out = _text(await seeded_server.call_tool("get_draft", {"post_id": 42}))
    assert out == "No draft"


@respx.mock
async def test_list_flows_tool_success(seeded_server):
    # list_flows renders the flows reference; the alias appears in the output.
    respx.get(f"{BASE_URL}refs/flows/wysiwyg").mock(
        return_value=httpx.Response(
            200, json={"flows": [{"id": "2", "alias": "backend", "title": "Бэкенд"}]}
        )
    )
    out = _text(await seeded_server.call_tool("list_flows", {}))
    assert "backend" in out


@respx.mock
async def test_list_flows_tool_error(seeded_server):
    # A Habr error dict surfaces as the error message string.
    respx.get(f"{BASE_URL}refs/flows/wysiwyg").mock(
        return_value=httpx.Response(200, json={"httpCode": 500, "message": "Down"})
    )
    out = _text(await seeded_server.call_tool("list_flows", {}))
    assert out == "Down"


_GDOC_BODY = {
    "body": {
        "content": [
            {"paragraph": {"elements": [
                {"textRun": {"content": "Тело\n", "textStyle": {}}}]}}
        ]
    }
}


@pytest.mark.parametrize("tool", ["create_draft_from_docmost", "create_draft_from_gdoc",
                                  "update_draft_from_docmost", "update_draft_from_gdoc"])
@respx.mock
async def test_save_habr_api_error_returns_message(tool, seeded_server, docmost_doc,
                                                   post_data_payload):
    # When the save route returns a Habr error dict, each draft tool returns the
    # error message string. Update tools also read post-data first (mocked valid).
    import json as json_module

    is_update = tool.startswith("update_")
    is_gdoc = tool.endswith("_from_gdoc")
    doc = _GDOC_BODY if is_gdoc else docmost_doc
    if is_update:
        respx.get(f"{BASE_URL}publication/post-data/42").mock(
            return_value=httpx.Response(200, json=post_data_payload)
        )
        respx.post(f"{BASE_URL}publication/save/42").mock(
            return_value=httpx.Response(200, json={"httpCode": 500, "message": "SaveErr"})
        )
        args = {"post_id": 42, "doc": json_module.dumps(doc),
                "announce": "А" * 120}
    else:
        respx.post(f"{BASE_URL}publication/save").mock(
            return_value=httpx.Response(200, json={"httpCode": 500, "message": "SaveErr"})
        )
        args = {"title": "T", "doc": json_module.dumps(doc), "hubs": ["161"],
                "tags": ["t1"], "flow": "2", "announce": "А" * 120}
    out = _text(await seeded_server.call_tool(tool, args))
    assert out == "SaveErr"
