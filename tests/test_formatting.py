"""Unit tests for the pure formatting helpers."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from src.formatting import (
    _author_alias,
    _truncate,
    format_article,
    format_article_list,
    format_comments,
    format_draft,
    format_drafts_list,
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


# --- format_draft --------------------------------------------------------


def test_format_draft_renders_all_meta_and_raw_sources(post_data_payload):
    # Full payload: every meta line plus both raw ProseMirror source blocks.
    out = format_draft(post_data_payload)
    assert "id: 1047360" in out
    assert "заголовок: Старый заголовок" in out
    assert "статус: drafted" in out
    assert "язык: ru" in out
    assert "тип: simple" in out
    assert "поток (flow): 2" in out
    assert "формат: common" in out
    # complexity is None in the fixture -> placeholder.
    assert "сложность: —" in out
    # hubs/tags rendered via the _list helper (raw int/str items).
    assert "хабы: 19791, 4992" in out
    assert "теги: t1" in out
    # publishedAt is None -> placeholder.
    assert "опубликовано: —" in out
    # Raw source blocks carry postForm.text.source / postForm.preview.source.
    assert "--- TEXT (ProseMirror source) ---\n" + '{"type":"doc","content":[]}' in out
    assert (
        "--- PREVIEW (ProseMirror source) ---\n" + '{"type":"doc","content":[]}' in out
    )


def test_format_draft_missing_postform_uses_payload_as_form():
    # No postForm key -> fall back to the payload itself as the form.
    out = format_draft({"id": "9", "title": "X"})
    assert "id: 9" in out
    assert "заголовок: X" in out


def test_format_draft_non_dict_postform_falls_back_to_placeholders():
    # postForm is not a dict -> form becomes {} -> all placeholders.
    out = format_draft({"postForm": 123})
    assert "id: ?" in out
    assert "заголовок: (без заголовка)" in out
    assert "статус: —" in out
    assert "хабы: —" in out
    assert "теги: —" in out


def test_format_draft_empty_hubs_and_tags_use_dash():
    # Empty hubs/tags lists -> placeholder "—".
    out = format_draft({"postForm": {"id": "1", "hubs": [], "tags": []}})
    assert "хабы: —" in out
    assert "теги: —" in out


def test_format_draft_non_dict_text_preview_yields_empty_sources():
    # Non-dict text/preview -> empty source strings, headers still present.
    out = format_draft({"postForm": {"id": "1", "text": 5, "preview": "nope"}})
    # Both section headers remain; TEXT header followed by an empty source.
    assert "--- TEXT (ProseMirror source) ---\n\n" in out
    assert out.rstrip().endswith("--- PREVIEW (ProseMirror source) ---")


# --- format_drafts_list --------------------------------------------------


def test_format_drafts_list_renders_order_and_fields(drafts_payload):
    # Renders in publicationIds order with id, title, flow alias, reading, hubs, tags.
    out = format_drafts_list(drafts_payload, "Черновики")
    assert "Черновики" in out
    # publicationIds order: 1052760 before 1052742.
    assert out.index("id=1052760") < out.index("id=1052742")
    assert "Разработка WB-MGE v.3" in out
    assert "поток industrial_engineering" in out
    assert "чтение 10 мин" in out
    assert "хабы: Умный дом" in out
    assert "теги: термопары" in out


def test_format_drafts_list_empty_says_no_drafts():
    # Empty list -> "Черновиков нет.".
    out = format_drafts_list({"publicationIds": [], "publicationRefs": {}}, "H")
    assert "Черновиков нет." in out


def test_format_drafts_list_non_dict_flow_shows_dash():
    # flowNew not a dict -> flow rendered as "—".
    payload = {
        "publicationIds": ["1"],
        "publicationRefs": {"1": {"id": "1", "flowNew": "oops"}},
    }
    out = format_drafts_list(payload, "H")
    assert "поток —" in out


def test_format_drafts_list_flow_alias_preferred_over_id():
    # A flowNew with both alias and id -> alias wins.
    payload = {
        "publicationIds": ["1"],
        "publicationRefs": {"1": {"id": "1", "flowNew": {"alias": "design", "id": "7"}}},
    }
    out = format_drafts_list(payload, "H")
    assert "поток design" in out
    assert "поток 7" not in out


def test_format_drafts_list_caps_hubs_at_3_and_tags_at_5():
    # >3 hubs / >5 tags -> only the cap is shown.
    payload = {
        "publicationIds": ["1"],
        "publicationRefs": {
            "1": {
                "id": "1",
                "hubs": [{"titleHtml": f"H{i}"} for i in range(5)],
                "tags": [{"titleHtml": f"T{i}"} for i in range(7)],
            }
        },
    }
    out = format_drafts_list(payload, "H")
    # First 3 hubs present, 4th cut.
    assert "H0" in out and "H2" in out
    assert "H3" not in out
    # First 5 tags present, 6th cut.
    assert "T4" in out
    assert "T5" not in out


def test_format_drafts_list_tags_string_and_dict_both_render():
    # tags as plain strings vs dicts both render.
    payload = {
        "publicationIds": ["1"],
        "publicationRefs": {"1": {"id": "1", "tags": ["a", {"titleHtml": "b"}]}},
    }
    out = format_drafts_list(payload, "H")
    assert "теги: a, b" in out


# --- _truncate / deleted comments via format_comments --------------------


def test_truncate_via_format_comments_long_message_ends_with_ellipsis():
    # A message longer than 280 chars must be truncated with a trailing "…".
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": 0,
                "message": "x" * 500,
                "author": {"alias": "carol"},
                "children": [],
            }
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    assert "…" in out
    # The full 500-char body must not leak.
    assert "x" * 500 not in out


def test_deleted_comment_via_deleted_flag_hides_message():
    # deleted: true -> body is "[удалён]" and the real text does not leak.
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": 0,
                "deleted": True,
                "message": "<p>СЕКРЕТ</p>",
                "author": {"alias": "carol"},
                "children": [],
            }
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    assert "[удалён]" in out
    assert "СЕКРЕТ" not in out


def test_deleted_comment_via_status_hides_message():
    # status: "deleted" -> body is "[удалён]" and the real text does not leak.
    payload = {
        "comments": {
            "1": {
                "id": "1",
                "level": 0,
                "status": "deleted",
                "message": "<p>СЕКРЕТ</p>",
                "author": {"alias": "carol"},
                "children": [],
            }
        },
        "threads": ["1"],
    }
    out = format_comments(payload, limit=100)
    assert "[удалён]" in out
    assert "СЕКРЕТ" not in out


def test_format_comments_break_at_root_thread_level():
    # Multiple root threads, limit < number of roots -> loop breaks at root level.
    payload = {
        "comments": {
            "1": {"id": "1", "level": 0, "message": "<p>R1</p>",
                  "author": {"alias": "u1"}, "children": []},
            "2": {"id": "2", "level": 0, "message": "<p>R2</p>",
                  "author": {"alias": "u2"}, "children": []},
            "3": {"id": "3", "level": 0, "message": "<p>R3</p>",
                  "author": {"alias": "u3"}, "children": []},
        },
        "threads": ["1", "2", "3"],
    }
    out = format_comments(payload, limit=2)
    # Only the first two roots render; the third is cut at the root loop.
    assert "@u1" in out
    assert "@u2" in out
    assert "@u3" not in out
    # Header counts stay consistent.
    assert "показано 2 из 3" in out


# --- _author_alias -------------------------------------------------------


def test_author_alias_variants():
    # alias wins; fullname is the fallback; non-dict / empty -> "?".
    assert _author_alias({"alias": "x"}) == "x"
    assert _author_alias({"fullname": "F"}) == "F"
    assert _author_alias("nope") == "?"
    assert _author_alias({}) == "?"


# --- format_article edge cases -------------------------------------------


def test_format_article_empty_data_uses_placeholders_and_omits_hub_tag_lines():
    # Empty data -> title/id/complexity placeholders; hubs/tags lines omitted.
    out = format_article({})
    assert "# (без заголовка)" in out
    assert "id: ?" in out
    assert "сложность: —" in out
    assert "хабы:" not in out
    assert "теги:" not in out


# --- html_to_text edge case ----------------------------------------------


def test_html_to_text_empty_returns_empty():
    # Empty input -> empty string.
    assert html_to_text("") == ""


# --- format_article_list edge cases --------------------------------------


def test_format_article_list_missing_ref_uses_placeholders_and_omits_pages():
    # An id with no ref -> placeholders; no pagesCount -> no "Всего страниц:" line.
    payload = {"publicationIds": ["100"], "publicationRefs": {}}
    out = format_article_list(payload, "Лента")
    assert "(без заголовка)" in out
    assert "@?" in out
    assert "Всего страниц:" not in out


# --- property: _truncate length invariant --------------------------------


@given(text=st.text(), limit=st.integers(min_value=1, max_value=500))
def test_truncate_length_never_exceeds_limit(text, limit):
    # Truncated form is (limit-1) chars + the single "…" char, so length == limit;
    # the non-truncated branch returns the stripped text whose length is already
    # <= limit. Either way len(result) <= limit.
    assert len(_truncate(text, limit)) <= limit
