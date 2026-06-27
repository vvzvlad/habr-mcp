"""Shared pytest fixtures: dummy settings and tiny fake Habr payloads."""

from __future__ import annotations

import pytest

from src.settings import Settings

BASE_URL = "https://habr.com/kek/v2/"


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
def auth_settings() -> Settings:
    """Settings with dummy write credentials."""
    return Settings(
        habr_lang="ru",
        habr_connect_sid="SID123",
        habr_csrf_token="CSRF456",
        habr_csrf_cookie_name="csrf_token",
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
def feed_payload() -> dict:
    """Two-article feed payload; publicationIds order is intentionally reversed."""
    return {
        "pagesCount": 5,
        "publicationIds": ["200", "100"],
        "publicationRefs": {
            "100": {
                "id": "100",
                "titleHtml": "Первая <b>статья</b>",
                "timePublished": "2026-01-01T10:00:00+00:00",
                "author": {"alias": "alice", "fullname": "Alice"},
                "statistics": {"score": 10, "commentsCount": 3},
                "readingTime": 5,
                "hubs": [{"titleHtml": "Python", "alias": "python"}],
            },
            "200": {
                "id": "200",
                "titleHtml": "Вторая статья",
                "timePublished": "2026-02-02T10:00:00+00:00",
                "author": {"alias": "bob", "fullname": "Bob"},
                "statistics": {"score": 20, "commentsCount": 7},
                "readingTime": 8,
                "hubs": [{"titleHtml": "Go", "alias": "go"}],
            },
        },
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
def comments_payload() -> dict:
    """A 2-level comment tree: one root with one child."""
    return {
        "comments": {
            "1": {
                "id": "1",
                "parentId": None,
                "level": 0,
                "timePublished": "2026-01-01T11:00:00+00:00",
                "score": 5,
                "message": "<p>Корневой комментарий</p>",
                "author": {"alias": "carol"},
                "children": ["2"],
            },
            "2": {
                "id": "2",
                "parentId": "1",
                "level": 1,
                "timePublished": "2026-01-01T12:00:00+00:00",
                "score": 2,
                "message": "<p>Ответ на корневой</p>",
                "author": {"alias": "dave"},
                "children": [],
            },
        },
        "threads": ["1"],
        "pinnedCommentIds": [],
    }
