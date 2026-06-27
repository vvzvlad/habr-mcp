"""Unit tests for the pure Docmost -> Habr ProseMirror converter."""

from __future__ import annotations

import json

from src.converter import (
    collect_image_srcs,
    docmost_to_habr_doc,
    make_preview_doc,
    serialize_source,
)


def _doc(*blocks: dict) -> dict:
    """Wrap blocks in a Docmost ProseMirror doc."""
    return {"type": "doc", "content": list(blocks)}


def _text(text: str, marks: list[dict] | None = None) -> dict:
    node: dict = {"type": "text", "text": text}
    if marks is not None:
        node["marks"] = marks
    return node


# --- paragraph ---------------------------------------------------------------


def test_paragraph_attrs_simple_persona_align():
    src = _doc(
        {"type": "paragraph", "attrs": {"textAlign": "center"}, "content": [_text("hi")]}
    )
    out = docmost_to_habr_doc(src)
    para = out["content"][0]
    assert para["type"] == "paragraph"
    assert para["attrs"] == {"align": "center", "simple": False, "persona": False}
    assert para["content"] == [{"type": "text", "text": "hi"}]


def test_paragraph_align_defaults_to_none():
    src = _doc({"type": "paragraph", "content": [_text("hi")]})
    para = docmost_to_habr_doc(src)["content"][0]
    assert para["attrs"]["align"] is None


def test_empty_trailing_paragraph_omits_content():
    src = _doc({"type": "paragraph"})
    para = docmost_to_habr_doc(src)["content"][0]
    assert "content" not in para
    assert para == {"type": "paragraph", "attrs": {"simple": False, "persona": False}}


# --- heading -----------------------------------------------------------------


def test_heading_level_clamp_low():
    src = _doc({"type": "heading", "attrs": {"level": 1}, "content": [_text("H")]})
    heading = docmost_to_habr_doc(src)["content"][0]
    assert heading["type"] == "heading"
    assert heading["attrs"] == {"level": 1, "class": None}


def test_heading_level_clamp_high():
    src = _doc({"type": "heading", "attrs": {"level": 5}, "content": [_text("H")]})
    heading = docmost_to_habr_doc(src)["content"][0]
    assert heading["attrs"]["level"] == 3


def test_heading_missing_level_defaults_to_one():
    src = _doc({"type": "heading", "content": [_text("H")]})
    heading = docmost_to_habr_doc(src)["content"][0]
    assert heading["attrs"]["level"] == 1


# --- inline: hard_break ------------------------------------------------------


def test_hardbreak_becomes_hard_break():
    src = _doc(
        {
            "type": "paragraph",
            "content": [_text("a"), {"type": "hardBreak"}, _text("b")],
        }
    )
    para = docmost_to_habr_doc(src)["content"][0]
    assert para["content"] == [
        {"type": "text", "text": "a"},
        {"type": "hard_break"},
        {"type": "text", "text": "b"},
    ]


# --- lists -------------------------------------------------------------------


def test_bullet_list_and_list_item():
    src = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("one")]}
                    ],
                }
            ],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "unordered_list"
    item = out["content"][0]
    assert item["type"] == "list_item"
    assert item["content"][0]["type"] == "paragraph"


def test_ordered_list():
    src = _doc(
        {
            "type": "orderedList",
            "content": [
                {"type": "listItem", "content": [{"type": "paragraph"}]}
            ],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "ordered_list"
    assert out["content"][0]["type"] == "list_item"


def test_list_item_with_code_block_preserves_content_and_warns():
    # A code block inside a list item is non-paragraph/non-list block content
    # Habr may reject: it must be kept (non-lossy) AND warned about once.
    src = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("intro")]},
                        {
                            "type": "codeBlock",
                            "attrs": {"language": "python"},
                            "content": [_text("print(1)")],
                        },
                    ],
                }
            ],
        }
    )
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)["content"][0]
    item = out["content"][0]
    assert item["type"] == "list_item"
    # Content preserved: paragraph + code_block both survive in order.
    inner_types = [block["type"] for block in item["content"]]
    assert inner_types == ["paragraph", "code_block"]
    assert item["content"][1]["attrs"]["code"] == "print(1)"
    # The warning is recorded exactly once.
    reject_warnings = [w for w in warnings if "list item contains block content" in w]
    assert len(reject_warnings) == 1


def test_list_item_with_only_paragraph_does_not_warn():
    # A plain paragraph-only list item is safe: no rejection warning.
    src = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [_text("plain")]}],
                }
            ],
        }
    )
    warnings: list[str] = []
    docmost_to_habr_doc(src, warnings=warnings)
    assert not any("list item contains block content" in w for w in warnings)


def test_task_list_degrades_with_single_warning():
    src = _doc(
        {
            "type": "taskList",
            "content": [
                {"type": "taskItem", "content": [{"type": "paragraph", "content": [_text("a")]}]},
                {"type": "taskItem", "content": [{"type": "paragraph", "content": [_text("b")]}]},
            ],
        }
    )
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)["content"][0]
    assert out["type"] == "unordered_list"
    assert all(item["type"] == "list_item" for item in out["content"])
    # The "once" warning is recorded a single time despite two task items.
    task_warnings = [w for w in warnings if "task list" in w]
    assert len(task_warnings) == 1


# --- code block --------------------------------------------------------------


def test_code_block_code_in_attrs_with_language():
    src = _doc(
        {
            "type": "codeBlock",
            "attrs": {"language": "bash"},
            "content": [_text("echo 1"), {"type": "hardBreak"}, _text("echo 2")],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "code_block"
    assert "content" not in out
    assert out["attrs"]["lang"] == "bash"
    assert out["attrs"]["code"] == "echo 1\necho 2"


def test_code_block_language_defaults_to_null():
    src = _doc({"type": "codeBlock", "content": [_text("x")]})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["attrs"]["lang"] is None


# --- marks -------------------------------------------------------------------


def test_mark_renames_sub_sup():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("x", [{"type": "subscript"}]),
                _text("y", [{"type": "superscript"}]),
            ],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline[0]["marks"] == [{"type": "sub"}]
    assert inline[1]["marks"] == [{"type": "sup"}]


def test_mark_bold_italic_and_link_href():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("b", [{"type": "bold"}, {"type": "italic"}]),
                _text(
                    "l",
                    [
                        {
                            "type": "link",
                            "attrs": {
                                "href": "https://x",
                                "target": "_blank",
                                "title": "t",
                            },
                        }
                    ],
                ),
            ],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline[0]["marks"] == [{"type": "bold"}, {"type": "italic"}]
    # Only href survives on the link mark.
    assert inline[1]["marks"] == [{"type": "link", "attrs": {"href": "https://x"}}]


def test_dropped_marks_keep_text():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("kept", [{"type": "highlight"}, {"type": "textStyle"}, {"type": "comment"}]),
            ],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    # Marks gone, "marks" key omitted, text preserved.
    assert inline[0] == {"type": "text", "text": "kept"}


def test_unknown_mark_dropped_with_warning():
    src = _doc(
        {"type": "paragraph", "content": [_text("z", [{"type": "weird"}])]}
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline[0] == {"type": "text", "text": "z"}
    assert any("unsupported mark dropped: weird" in w for w in warnings)


# --- callout / details -> spoiler --------------------------------------------


def test_callout_warning_becomes_spoiler_with_russian_title():
    src = _doc(
        {
            "type": "callout",
            "attrs": {"type": "warning"},
            "content": [{"type": "paragraph", "content": [_text("careful")]}],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "spoiler"
    # Russian label, not the English "Warning".
    assert out["attrs"]["title"] == "Внимание"
    assert out["content"][0]["type"] == "paragraph"


def test_callout_known_types_map_to_russian_titles():
    cases = {
        "info": "Примечание",
        "warning": "Внимание",
        "danger": "Важно",
        "success": "Готово",
    }
    for callout_type, expected in cases.items():
        src = _doc(
            {
                "type": "callout",
                "attrs": {"type": callout_type},
                "content": [{"type": "paragraph"}],
            }
        )
        out = docmost_to_habr_doc(src)["content"][0]
        assert out["attrs"]["title"] == expected


def test_callout_type_lookup_is_case_insensitive():
    src = _doc(
        {
            "type": "callout",
            "attrs": {"type": "WARNING"},
            "content": [{"type": "paragraph"}],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["attrs"]["title"] == "Внимание"


def test_callout_without_type_uses_default_title():
    src = _doc({"type": "callout", "content": [{"type": "paragraph"}]})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["attrs"]["title"] == "Спойлер"


def test_callout_unknown_type_uses_default_title():
    src = _doc(
        {"type": "callout", "attrs": {"type": "mystery"}, "content": [{"type": "paragraph"}]}
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["attrs"]["title"] == "Спойлер"


def test_details_becomes_spoiler_using_summary_text():
    src = _doc(
        {
            "type": "details",
            "content": [
                {"type": "detailsSummary", "content": [_text("Подробности")]},
                {
                    "type": "detailsContent",
                    "content": [{"type": "paragraph", "content": [_text("body")]}],
                },
            ],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "spoiler"
    assert out["attrs"]["title"] == "Подробности"
    assert out["content"][0]["type"] == "paragraph"
    assert out["content"][0]["content"][0]["text"] == "body"


# --- images ------------------------------------------------------------------


def test_image_present_in_map_emitted_with_fullwidth_and_caption():
    src = _doc(
        {
            "type": "image",
            "attrs": {"src": "orig://a", "alt": "A", "width": "100", "height": 200},
        }
    )
    out = docmost_to_habr_doc(src, image_url_map={"orig://a": "https://habrastorage/a.jpg"})
    img = out["content"][0]
    assert img["type"] == "image"
    assert img["attrs"]["src"] == "https://habrastorage/a.jpg"
    assert img["attrs"]["alt"] == "A"
    assert img["attrs"]["width"] == 100  # coerced from string
    assert img["attrs"]["height"] == 200
    assert img["attrs"]["title"] is None
    assert img["attrs"]["fullWidth"] is True
    assert img["content"] == [{"type": "image_caption"}]


def test_image_not_in_map_dropped_with_warning():
    src = _doc({"type": "image", "attrs": {"src": "orig://missing"}})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, image_url_map={}, warnings=warnings)
    assert out["content"] == []
    assert any("image dropped (no habrastorage url): orig://missing" in w for w in warnings)


def test_image_none_map_dropped():
    src = _doc({"type": "image", "attrs": {"src": "orig://x"}})
    out = docmost_to_habr_doc(src, image_url_map=None)
    assert out["content"] == []


# --- unknown blocks ----------------------------------------------------------


def test_unknown_block_atom_dropped_with_warning():
    src = _doc({"type": "drawio", "attrs": {"id": "1"}})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    assert out["content"] == []
    assert any("unsupported block dropped: drawio" in w for w in warnings)


def test_unknown_wrapper_block_flattened_preserves_children():
    # A table wrapper: unknown to Habr, but its paragraph children survive.
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableCell",
                            "content": [{"type": "paragraph", "content": [_text("cell")]}],
                        }
                    ],
                }
            ],
        }
    )
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    # The nested paragraph is spliced up into the document.
    assert len(out["content"]) == 1
    para = out["content"][0]
    assert para["type"] == "paragraph"
    assert para["content"][0]["text"] == "cell"
    assert any("unsupported block flattened: table" in w for w in warnings)


# --- other block nodes -------------------------------------------------------


def test_blockquote_and_hr():
    src = _doc(
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [_text("q")]}]},
        {"type": "horizontalRule"},
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["type"] == "blockquote"
    assert out[0]["content"][0]["type"] == "paragraph"
    assert out[1] == {"type": "hr", "attrs": {"inserted": True}}


# --- image collection --------------------------------------------------------


def test_collect_image_srcs_dedup_in_order():
    src = _doc(
        {"type": "image", "attrs": {"src": "a"}},
        {"type": "paragraph", "content": [_text("x")]},
        {"type": "image", "attrs": {"src": "b"}},
        {"type": "image", "attrs": {"src": "a"}},  # duplicate
        {"type": "image", "attrs": {}},  # no src, skipped
    )
    assert collect_image_srcs(src) == ["a", "b"]


# --- _as_doc normalization ---------------------------------------------------


def test_as_doc_normalization_wrapper_with_content_doc():
    # A Docmost get_page_json-style wrapper holding the doc under "content".
    inner = _doc({"type": "paragraph", "content": [_text("wrapped")]})
    wrapper = {"id": "page-123", "title": "Page", "content": inner}
    out = docmost_to_habr_doc(wrapper)
    assert out["content"][0]["content"][0]["text"] == "wrapped"


def test_as_doc_accepts_json_string():
    src = json.dumps(_doc({"type": "paragraph", "content": [_text("s")]}))
    out = docmost_to_habr_doc(src)
    assert out["content"][0]["content"][0]["text"] == "s"


def test_as_doc_wraps_bare_content_list():
    wrapper = {"content": [{"type": "paragraph", "content": [_text("bare")]}]}
    out = docmost_to_habr_doc(wrapper)
    assert out["content"][0]["content"][0]["text"] == "bare"


# --- serialize_source --------------------------------------------------------


def test_serialize_source_roundtrips():
    habr = docmost_to_habr_doc(_doc({"type": "paragraph", "content": [_text("hi")]}))
    s = serialize_source(habr)
    assert isinstance(s, str)
    # Compact separators (no spaces after , or :).
    assert ", " not in s
    assert ": " not in s
    assert json.loads(s) == habr


def test_serialize_source_keeps_unicode():
    habr = docmost_to_habr_doc(_doc({"type": "paragraph", "content": [_text("привет")]}))
    s = serialize_source(habr)
    assert "привет" in s  # ensure_ascii=False


# --- make_preview_doc --------------------------------------------------------


def test_make_preview_doc_single_nonempty_paragraph():
    habr = docmost_to_habr_doc(
        _doc(
            {"type": "heading", "attrs": {"level": 1}, "content": [_text("Заголовок")]},
            {"type": "paragraph", "content": [_text("Тело")]},
        )
    )
    preview = make_preview_doc(habr)
    assert preview["type"] == "doc"
    assert len(preview["content"]) == 1
    para = preview["content"][0]
    assert para["type"] == "paragraph"
    # First heading text is used as the announce.
    assert para["content"][0]["text"] == "Заголовок"


def test_make_preview_doc_falls_back_when_no_text():
    habr = {"type": "doc", "content": []}
    preview = make_preview_doc(habr)
    assert preview["content"][0]["content"][0]["text"] == "Читать далее"


def test_make_preview_doc_trims_long_text():
    long_text = "x" * 500
    habr = docmost_to_habr_doc(_doc({"type": "paragraph", "content": [_text(long_text)]}))
    preview = make_preview_doc(habr)
    assert len(preview["content"][0]["content"][0]["text"]) <= 220
