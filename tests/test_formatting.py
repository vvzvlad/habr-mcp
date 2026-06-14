"""Unit tests for the pure formatting helpers."""

from __future__ import annotations

from src.formatting import (
    format_article,
    format_article_list,
    format_comments,
    html_to_markdown,
    html_to_text,
)


def test_html_to_markdown_headings_and_blanklines():
    md = html_to_markdown("<h2>Title</h2><p>Body</p>")
    assert "## Title" in md
    assert "Body" in md
    # No runs of 3+ newlines remain.
    assert "\n\n\n" not in md


def test_html_to_markdown_empty():
    assert html_to_markdown("") == ""


def test_html_to_text_flattens_whitespace():
    text = html_to_text("<p>Hello\n   world</p><p>!</p>")
    assert "Hello world" in text
    assert "\n" not in text


def test_format_article_list_follows_publication_ids_order(feed_payload):
    out = format_article_list(feed_payload, "Лента")
    # publicationIds is ["200", "100"]; item 200 ("Вторая") must come first.
    pos_second = out.index("Вторая статья")
    pos_first = out.index("Первая")
    assert pos_second < pos_first
    # Metadata pieces present.
    assert "Всего страниц: 5" in out
    assert "id=200" in out
    assert "@bob" in out
    assert "рейтинг 20" in out


def test_format_article_list_empty():
    out = format_article_list({"publicationIds": [], "publicationRefs": {}}, "H")
    assert "Ничего не найдено" in out


def test_format_article_renders_meta_and_body(article_payload):
    out = format_article(article_payload)
    assert "# Заголовок статьи" in out
    assert "id: 100" in out
    assert "@alice" in out
    assert "рейтинг: 42 (+50 / -8)" in out
    assert "сложность: medium" in out
    assert "https://habr.com/ru/articles/100/" in out
    assert "---" in out
    # Body converted from HTML to Markdown.
    assert "## Раздел" in out
    assert "жирным" in out


def test_format_comments_builds_indented_tree(comments_payload):
    out = format_comments(comments_payload, limit=100)
    assert "@carol" in out
    assert "@dave" in out
    assert "Корневой комментарий" in out
    assert "Ответ на корневой" in out
    # Child (level 1) is indented by two spaces relative to root marker.
    assert "  — @dave" in out
    # Root marker has no leading indent.
    assert "— @carol" in out


def test_format_comments_respects_limit(comments_payload):
    out = format_comments(comments_payload, limit=1)
    assert "@carol" in out
    # Child should be cut off by the limit.
    assert "@dave" not in out
    assert "показаны первые 1 из 2" in out


def test_format_comments_empty():
    assert format_comments({"comments": {}, "threads": []}, 10) == "Комментариев нет."


def test_format_comments_string_level_does_not_crash():
    # Habr can return numeric fields as strings; level="1" must indent, not crash.
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": "1",
                "timePublished": "2026-01-01T11:00:00+00:00",
                "score": 5,
                "message": "<p>Строковый уровень</p>",
                "author": {"alias": "carol"},
                "children": [],
            }
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    assert "Строковый уровень" in out
    # level "1" -> two-space indent before the marker.
    assert "  — @carol" in out


def test_format_comments_garbage_level_falls_back_to_zero():
    # A non-numeric level must fall back to 0 (no indent), not raise.
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": "deep",
                "message": "<p>Мусорный уровень</p>",
                "author": {"alias": "carol"},
                "children": [],
            }
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    assert "— @carol" in out
    assert "  — @carol" not in out


def test_format_comments_cyclic_graph_terminates_and_dedups():
    # Cyclic children 1->2->1: must terminate, render each once, sane counts.
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": 0,
                "message": "<p>Первый</p>",
                "author": {"alias": "carol"},
                "children": ["2"],
            },
            "2": {
                "id": "2",
                "level": 1,
                "message": "<p>Второй</p>",
                "author": {"alias": "dave"},
                "children": ["1"],
            },
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    # Each comment rendered exactly once.
    assert out.count("@carol") == 1
    assert out.count("@dave") == 1
    # Header count must not exceed total, and no false "truncated" note.
    assert "показано 2 из 2" in out
    assert "показаны первые" not in out


def test_format_comments_truncation_note_when_total_exceeds_limit():
    # Three comments, limit 2: header shows 2 of 3 and a truncation note appears.
    payload = {
        "comments": {
            "1": {"id": "1", "level": 0, "message": "<p>A</p>",
                  "author": {"alias": "u1"}, "children": ["2"]},
            "2": {"id": "2", "level": 1, "message": "<p>B</p>",
                  "author": {"alias": "u2"}, "children": ["3"]},
            "3": {"id": "3", "level": 2, "message": "<p>C</p>",
                  "author": {"alias": "u3"}, "children": []},
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=2)
    assert "показано 2 из 3" in out
    assert "показаны первые 2 из 3 комментариев" in out
    # The third comment must be cut off by the limit.
    assert "@u3" not in out
