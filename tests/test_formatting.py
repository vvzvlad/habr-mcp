"""Unit tests for the pure formatting helpers."""

from __future__ import annotations

from src.formatting import (
    _author_alias,
    format_article,
    format_draft,
    format_drafts_list,
    format_me,
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


# --- format_me -----------------------------------------------------------


def test_format_me_renders_identity():
    # Full payload: identity, profile URL, karma (scoreStats.score) and rating.
    out = format_me({
        "id": "12345",
        "alias": "vvzvlad",
        "fullname": "VVZVlad",
        "speciality": "Маг",
        "rating": 42.5,
        "scoreStats": {"score": 100},
        "registerDateTime": "2015-03-01T12:00:00+00:00",
    })
    assert "@vvzvlad" in out
    assert "VVZVlad" in out
    assert "id: 12345" in out
    assert "https://habr.com/ru/users/vvzvlad/" in out
    assert "карма: 100" in out
    assert "рейтинг: 42.5" in out


def test_format_me_minimal_alias_only_does_not_crash():
    # Minimal payload with just an alias -> renders @alias, no exception.
    out = format_me({"alias": "x"})
    assert "@x" in out


def test_format_me_empty_has_no_profile_line():
    # Empty dict -> still a string; without an alias there is no profile line.
    out = format_me({})
    assert isinstance(out, str)
    assert "профиль:" not in out


def test_format_me_none_does_not_raise():
    # Defensive: a non-dict (None) must not raise.
    out = format_me(None)
    assert isinstance(out, str)


# --- html_to_text edge case ----------------------------------------------


def test_html_to_text_empty_returns_empty():
    # Empty input -> empty string.
    assert html_to_text("") == ""
