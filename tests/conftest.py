"""Shared pytest fixtures: dummy settings and tiny fake Habr payloads."""

from __future__ import annotations

import httpx
import pytest
import respx

from src.settings import Settings

BASE_URL = "https://habr.com/kek/v2/"


@pytest.fixture(autouse=True)
def default_app_version_probe():
    """Provide a default `me` response so the author bootstrap probe succeeds.

    Author endpoints now learn `x-app-version` from Habr on first use via a one-off
    GET to `me` (see HabrClient._ensure_app_version). This autouse fixture registers
    a default `me` route carrying `server-habr-version` so existing author tests do
    not hit respx's "not mocked" error during that probe. It composes with each
    test's own `@respx.mock` router; a test that registers its own `me` route
    overrides this default. Unused, it is harmless (assert_all_called is off).
    """
    respx.get(f"{BASE_URL}me").mock(
        return_value=httpx.Response(
            200, json={"alias": "x"}, headers={"server-habr-version": "2.329.0"}
        )
    )
    yield


@pytest.fixture
def anon_settings() -> Settings:
    """Settings with no credentials (read-only)."""
    return Settings(
        habr_lang="ru",
        habr_connect_sid=None,
        habr_csrf_token=None,
        proxy=None,
        per_page=20,
    )


@pytest.fixture
def author_settings() -> Settings:
    """Settings with dummy author (draft) credentials."""
    return Settings(
        habr_lang="ru",
        habr_cookie="connect_sid=s%3Aabc; hsec_id=def; habrsession_id=ghi",
        habr_csrf_token="CSRF456",
        habr_user_uuid="uuid-1",
        proxy=None,
        per_page=20,
    )


@pytest.fixture
def docmost_doc() -> dict:
    """A tiny image-free Docmost ProseMirror doc with one paragraph."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Привет, Хабр."}],
            }
        ],
    }


@pytest.fixture
def gdoc_doc() -> dict:
    """A tiny image-free Google Docs "Document" with one paragraph."""
    return {
        "title": "Doc",
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {"textRun": {"content": "Привет, Хабр.\n", "textStyle": {}}}
                        ]
                    }
                }
            ]
        },
    }


@pytest.fixture
def post_data_payload() -> dict:
    """A ``post-data`` response with read-side types (hubs ints, editorVersion str)."""
    return {
        "postForm": {
            "id": "1047360",
            "lang": "ru",
            "type": "simple",
            "status": "drafted",
            "title": "Старый заголовок",
            "hubs": [19791, 4992],
            "tags": ["t1"],
            "flow": "2",
            "format": "common",
            "complexity": None,
            "publishedAt": None,
            "text": {
                "source": '{"type":"doc","content":[]}',
                "editorVersion": "2",
                "isMarkdown": False,
            },
            "preview": {
                "source": '{"type":"doc","content":[]}',
                "editorVersion": "2",
                "isMarkdown": False,
            },
        }
    }


@pytest.fixture
def article_payload() -> dict:
    """A single full article payload."""
    return {
        "id": "100",
        "titleHtml": "Заголовок <i>статьи</i>",
        "timePublished": "2026-01-01T10:00:00+00:00",
        "author": {"alias": "alice", "fullname": "Alice"},
        "statistics": {"score": 42, "votesCountPlus": 50, "votesCountMinus": 8},
        "readingTime": 7,
        "complexity": "medium",
        "hubs": [{"titleHtml": "Python", "alias": "python"}],
        "tags": [{"titleHtml": "mcp"}, {"titleHtml": "api"}],
        "textHtml": "<h2>Раздел</h2><p>Текст статьи с <b>жирным</b>.</p>",
    }


@pytest.fixture
def drafts_payload() -> dict:
    """A drafts list payload (articles/drafts shape) with two article drafts."""
    return {
        "pagesCount": 1,
        "publicationIds": ["1052760", "1052742"],
        "publicationRefs": {
            "1052760": {
                "id": "1052760",
                "titleHtml": "Разработка WB-MGE v.3",
                "status": "draft",
                "timePublished": None,
                "author": {"alias": "sangman1987", "fullname": "Игорь"},
                "statistics": {"score": 0, "commentsCount": 0},
                "readingTime": 10,
                "hubs": [{"titleHtml": "Умный дом", "alias": "home_automation"}],
                "flowNew": {"id": "14", "alias": "industrial_engineering",
                            "title": "Промышленная инженерия"},
                "tags": [],
            },
            "1052742": {
                "id": "1052742",
                "titleHtml": "Отказы термопар",
                "status": "draft",
                "timePublished": None,
                "author": {"alias": "sangman1987", "fullname": "Игорь"},
                "statistics": {"score": 0, "commentsCount": 0},
                "readingTime": 13,
                "hubs": [{"titleHtml": "DIY или Сделай сам", "alias": "DIY"}],
                "flowNew": {"id": "2", "alias": "backend", "title": "Бэкенд"},
                "tags": [{"titleHtml": "термопары"}],
            },
        },
    }
