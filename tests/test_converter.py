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


def test_paragraph_omits_align_when_null():
    # Habr's canonical paragraph has NO align key when alignment is null.
    src = _doc({"type": "paragraph", "content": [_text("hi")]})
    para = docmost_to_habr_doc(src)["content"][0]
    assert para["attrs"] == {"simple": False, "persona": False}
    assert "align" not in para["attrs"]


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


def test_heading_lone_h5_normalizes_to_level_one():
    # With doc-wide normalization a LONE H5 is the document's top heading, so its
    # min level (5) becomes Habr level 1 -- not clamped down to 3.
    src = _doc({"type": "heading", "attrs": {"level": 5}, "content": [_text("H")]})
    heading = docmost_to_habr_doc(src)["content"][0]
    assert heading["attrs"]["level"] == 1


def test_heading_level_clamp_high():
    # A real upper-clamp case: min=1 (H1), so H7 normalizes to 7 then clamps to 3.
    src = _doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [_text("A")]},
        {"type": "heading", "attrs": {"level": 7}, "content": [_text("B")]},
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["attrs"]["level"] == 1
    assert out[1]["attrs"]["level"] == 3


def test_heading_top_h2_h3_normalize_to_one_and_two():
    # Key regression: a body that starts at H2 must yield Habr level-1 headings.
    src = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("A")]},
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("B")]},
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["attrs"]["level"] == 1
    assert out[1]["attrs"]["level"] == 2


def test_heading_h1_h2_h3_normalize_to_one_two_three():
    src = _doc(
        {"type": "heading", "attrs": {"level": 1}, "content": [_text("A")]},
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("B")]},
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("C")]},
    )
    out = docmost_to_habr_doc(src)["content"]
    assert [h["attrs"]["level"] for h in out] == [1, 2, 3]


def test_heading_only_h3_normalizes_to_one():
    src = _doc({"type": "heading", "attrs": {"level": 3}, "content": [_text("H")]})
    heading = docmost_to_habr_doc(src)["content"][0]
    assert heading["attrs"]["level"] == 1


def test_heading_h2_h5_gap_normalizes_to_one_and_clamped_three():
    # min=2; H5 -> 5-2+1 = 4 -> clamped to 3.
    src = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("A")]},
        {"type": "heading", "attrs": {"level": 5}, "content": [_text("B")]},
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["attrs"]["level"] == 1
    assert out[1]["attrs"]["level"] == 3


def test_heading_nested_in_callout_uses_doc_wide_min_level():
    # Top-level H2 sets the baseline (min=2); an H3 inside a callout/spoiler must
    # normalize against that same doc-wide min -> level 2, not 1.
    src = _doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [_text("Top")]},
        {
            "type": "callout",
            "attrs": {"type": "info"},
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [_text("Inside")],
                }
            ],
        },
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["type"] == "heading"
    assert out[0]["attrs"]["level"] == 1
    spoiler = out[1]
    assert spoiler["type"] == "spoiler"
    inner_heading = spoiler["content"][0]
    assert inner_heading["type"] == "heading"
    assert inner_heading["attrs"]["level"] == 2


def test_heading_in_table_cell_does_not_drag_min_level_down():
    # A top-level H3 plus a table whose cell contains a heading level 1: the
    # in-cell heading is flattened to text and must NOT lower the baseline, so the
    # top-level H3 (min=3) still normalizes to level 1.
    src = _doc(
        {"type": "heading", "attrs": {"level": 3}, "content": [_text("Top")]},
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableCell",
                            "content": [
                                {
                                    "type": "heading",
                                    "attrs": {"level": 1},
                                    "content": [_text("Cell H1")],
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )
    out = docmost_to_habr_doc(src)["content"]
    assert out[0]["type"] == "heading"
    assert out[0]["attrs"]["level"] == 1


def test_no_headings_doc_converts_without_error():
    src = _doc({"type": "paragraph", "content": [_text("just text")]})
    out = docmost_to_habr_doc(src)
    assert out["content"][0]["type"] == "paragraph"


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
    # Top-level list carries attrs.type "outer".
    assert out["attrs"] == {"type": "outer"}
    item = out["content"][0]
    # Habr's item node is "listitem" (one word), with NO attrs.
    assert item["type"] == "listitem"
    assert "attrs" not in item
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
    assert out["attrs"] == {"type": "outer"}
    assert out["content"][0]["type"] == "listitem"


def test_list_emits_listitem_never_snake_case_in_source():
    # The serialized text.source must use "listitem", never "list_item".
    src = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [_text("x")]}],
                }
            ],
        }
    )
    source = serialize_source(docmost_to_habr_doc(src))
    assert "listitem" in source
    assert "list_item" not in source


def test_nested_list_inside_list_item_is_inner():
    # A list directly inside a listitem is tagged attrs.type "inner"; the outer
    # list stays "outer".
    src = _doc(
        {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [
                        {"type": "paragraph", "content": [_text("outer item")]},
                        {
                            "type": "bulletList",
                            "content": [
                                {
                                    "type": "listItem",
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [_text("nested")],
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
    )
    warnings: list[str] = []
    outer = docmost_to_habr_doc(src, warnings=warnings)["content"][0]
    assert outer["type"] == "unordered_list"
    assert outer["attrs"] == {"type": "outer"}
    item = outer["content"][0]
    assert item["type"] == "listitem"
    inner_list = item["content"][1]
    assert inner_list["type"] == "unordered_list"
    assert inner_list["attrs"] == {"type": "inner"}
    # A nested list is safe content inside a list item: no rejection warning.
    assert not any("list item contains block content" in w for w in warnings)


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
    assert item["type"] == "listitem"
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
    assert all(item["type"] == "listitem" for item in out["content"])
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


# --- mentions ----------------------------------------------------------------


_MENTION_ATTRS = {
    "identity": "vvzvlad",
    "identityType": "user",
    "display": "@vvzvlad",
    "link": "/users/vvzvlad",
    "class": "mention",
}


def test_docmost_mention_node_user_becomes_habr_mention():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                {"type": "mention", "attrs": {"label": "@vvzvlad", "entityType": "user"}}
            ],
        }
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline == [{"type": "mention", "attrs": _MENTION_ATTRS}]
    # A real mention emits no "converted to plain text" warning.
    assert not any("mention converted to plain text" in w for w in warnings)


def test_docmost_mention_node_page_falls_back_to_text():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "mention",
                    "attrs": {"label": "Some Page", "entityType": "page"},
                }
            ],
        }
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "Some Page"}]
    assert any("mention converted to plain text" in w for w in warnings)


def test_docmost_mention_node_multiword_label_falls_back_to_text():
    # A multi-word display label is not a single-token nick: plain-text fallback.
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                {
                    "type": "mention",
                    "attrs": {"label": "@John Doe", "entityType": "user"},
                }
            ],
        }
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "@John Doe"}]
    assert any("mention converted to plain text" in w for w in warnings)


def test_plain_text_at_nick_becomes_mention_between_text():
    src = _doc(
        {
            "type": "paragraph",
            "content": [_text("Спросите @vvzvlad про это")],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [
        {"type": "text", "text": "Спросите "},
        {"type": "mention", "attrs": _MENTION_ATTRS},
        {"type": "text", "text": " про это"},
    ]


def test_plain_text_mention_keeps_marks_on_surrounding_text_only():
    # The text node is bold: surrounding literal segments keep the bold mark,
    # but the mention node itself carries NO marks.
    src = _doc(
        {
            "type": "paragraph",
            "content": [_text("ping @vvzvlad now", [{"type": "bold"}])],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [
        {"type": "text", "text": "ping ", "marks": [{"type": "bold"}]},
        {"type": "mention", "attrs": _MENTION_ATTRS},
        {"type": "text", "text": " now", "marks": [{"type": "bold"}]},
    ]


def test_email_like_text_produces_no_mention():
    src = _doc({"type": "paragraph", "content": [_text("написал user@example.com")]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "написал user@example.com"}]


def test_doubled_at_produces_no_mention():
    src = _doc({"type": "paragraph", "content": [_text("look @@x here")]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "look @@x here"}]


def test_short_nick_produces_no_mention():
    # A 1-char nick ("@a") is below the 2-char minimum: stays literal.
    src = _doc({"type": "paragraph", "content": [_text("hi @a there")]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "hi @a there"}]


def test_code_mark_text_with_at_stays_literal():
    # An inline code span containing "@media" must NOT become a mention.
    src = _doc(
        {
            "type": "paragraph",
            "content": [_text("@media", [{"type": "code"}])],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "@media", "marks": [{"type": "code"}]}]


def test_link_mark_text_with_at_stays_single_linked_node():
    # A ``@nick`` inside linked text must keep its href: stay one text node with
    # the link mark, NOT split into a (mark-less) mention that loses the link.
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text(
                    "ask @vvzvlad here",
                    [{"type": "link", "attrs": {"href": "https://x"}}],
                )
            ],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [
        {
            "type": "text",
            "text": "ask @vvzvlad here",
            "marks": [{"type": "link", "attrs": {"href": "https://x"}}],
        }
    ]
    # No mention node was produced inside the link.
    assert all(node["type"] != "mention" for node in inline)


def test_mention_nick_length_boundary():
    # Documents the _MENTION_RE boundary: a nick of 2..30 word chars matches; a
    # 31-char run does not (the regex caps the identity length at 30).
    nick30 = "a" * 30
    src = _doc({"type": "paragraph", "content": [_text("@" + nick30)]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [
        {
            "type": "mention",
            "attrs": {
                "identity": nick30,
                "identityType": "user",
                "display": "@" + nick30,
                "link": "/users/" + nick30,
                "class": "mention",
            },
        }
    ]

    nick31 = "a" * 31
    src = _doc({"type": "paragraph", "content": [_text("@" + nick31)]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    # Over the 30-char cap: no mention, stays literal text.
    assert inline == [{"type": "text", "text": "@" + nick31}]


def test_plain_prose_without_at_has_no_spurious_mention():
    # Regression: ordinary prose with no @ yields a single unchanged text node.
    src = _doc({"type": "paragraph", "content": [_text("just normal prose here")]})
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "text", "text": "just normal prose here"}]


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
    # A "columns" wrapper: unknown to Habr, but its paragraph children survive.
    src = _doc(
        {
            "type": "columns",
            "content": [
                {
                    "type": "column",
                    "content": [
                        {"type": "paragraph", "content": [_text("cell")]}
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
    assert any("unsupported block flattened: columns" in w for w in warnings)


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


# --- table -------------------------------------------------------------------


def _table_cell(cell_type: str, text: str | None, **attrs: object) -> dict:
    """Build a Docmost table cell (tableCell/tableHeader) holding one paragraph."""
    content: list[dict] = []
    if text is not None:
        content.append({"type": "paragraph", "content": [_text(text)]})
    elif text is None:
        # An empty cell: a paragraph with no content.
        content.append({"type": "paragraph"})
    node: dict = {"type": cell_type, "content": content}
    if attrs:
        node["attrs"] = attrs
    return node


def test_table_maps_to_table_wrapper_with_cells_and_paragraphs():
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        _table_cell("tableHeader", "H1"),
                        _table_cell("tableHeader", "H2"),
                    ],
                },
                {
                    "type": "tableRow",
                    "content": [
                        _table_cell("tableCell", "A1"),
                        _table_cell("tableCell", "B1"),
                    ],
                },
            ],
        }
    )
    out = docmost_to_habr_doc(src)["content"][0]
    assert out["type"] == "table_wrapper"
    table = out["content"][0]
    assert table["type"] == "table"
    rows = table["content"]
    assert [r["type"] for r in rows] == ["table_row", "table_row"]
    # Header row: tableHeader maps to table_cell (Habr has no header cell).
    header_cells = rows[0]["content"]
    assert [c["type"] for c in header_cells] == ["table_cell", "table_cell"]
    first = header_cells[0]
    assert first["attrs"] == {"colspan": 1, "rowspan": 1, "colwidth": None}
    para = first["content"][0]
    assert para["type"] == "table_paragraph"
    assert para["attrs"] == {"align": None}
    assert para["content"] == [{"type": "text", "text": "H1"}]
    # Body row cell text preserved.
    assert rows[1]["content"][0]["content"][0]["content"][0]["text"] == "A1"


def test_table_cell_colspan_rowspan_colwidth_coerced():
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        _table_cell(
                            "tableCell", "x", colspan="2", rowspan=3, colwidth=[120]
                        )
                    ],
                }
            ],
        }
    )
    cell = docmost_to_habr_doc(src)["content"][0]["content"][0]["content"][0]["content"][0]
    # colspan/rowspan coerced to int; colwidth passed through as-is.
    assert cell["attrs"] == {"colspan": 2, "rowspan": 3, "colwidth": [120]}


def test_table_empty_cell_yields_one_empty_table_paragraph():
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [_table_cell("tableCell", None)],  # empty paragraph
                }
            ],
        }
    )
    cell = docmost_to_habr_doc(src)["content"][0]["content"][0]["content"][0]["content"][0]
    assert len(cell["content"]) == 1
    empty_para = cell["content"][0]
    assert empty_para["type"] == "table_paragraph"
    assert empty_para["attrs"] == {"align": None}
    assert "content" not in empty_para


def test_table_cell_with_no_paragraphs_gets_empty_table_paragraph():
    # A cell whose only child is something with no text still gets one cell para.
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [{"type": "tableCell", "content": []}],
                }
            ],
        }
    )
    cell = docmost_to_habr_doc(src)["content"][0]["content"][0]["content"][0]["content"][0]
    assert cell["content"] == [{"type": "table_paragraph", "attrs": {"align": None}}]


def test_table_complex_cell_content_flattened_with_warning():
    # A nested list inside a cell flattens to a table_paragraph of its text.
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [
                        {
                            "type": "tableCell",
                            "content": [
                                {
                                    "type": "bulletList",
                                    "content": [
                                        {
                                            "type": "listItem",
                                            "content": [
                                                {
                                                    "type": "paragraph",
                                                    "content": [_text("li-text")],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    warnings: list[str] = []
    cell = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"][0]["content"][0]["content"][0]
    para = cell["content"][0]
    assert para["type"] == "table_paragraph"
    assert para["content"] == [{"type": "text", "text": "li-text"}]
    assert any("complex table cell content flattened" in w for w in warnings)


def test_table_with_no_rows_dropped_with_warning():
    src = _doc({"type": "table", "content": []})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    assert out["content"] == []
    assert any("empty table dropped" in w for w in warnings)


def test_table_row_with_no_valid_cells_skipped_not_emitted_empty():
    # A tableRow whose children yield no cells must be skipped (with a warning),
    # not emitted as a table_row with content: []. The valid row still survives.
    src = _doc(
        {
            "type": "table",
            "content": [
                # Row with no recognizable cells (only a stray non-cell child).
                {
                    "type": "tableRow",
                    "content": [{"type": "paragraph", "content": [_text("stray")]}],
                },
                # A valid row so the table itself is not dropped as empty.
                {
                    "type": "tableRow",
                    "content": [_table_cell("tableCell", "ok")],
                },
            ],
        }
    )
    warnings: list[str] = []
    table = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"][0]
    rows = table["content"]
    # Only the valid row is emitted; the empty one is skipped (no content: []).
    assert len(rows) == 1
    assert rows[0]["type"] == "table_row"
    assert rows[0]["content"][0]["type"] == "table_cell"
    assert any("empty table row skipped" in w for w in warnings)


# --- math blocks / inline formula --------------------------------------------


def test_math_block_becomes_formula():
    src = _doc({"type": "mathBlock", "attrs": {"text": "a^2"}})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out == {"type": "formula", "attrs": {"source": "a^2"}}


def test_empty_math_block_dropped_with_warning():
    src = _doc({"type": "mathBlock", "attrs": {"text": ""}})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    assert out["content"] == []
    assert any("empty mathBlock dropped" in w for w in warnings)


def test_whitespace_only_math_block_dropped_with_warning():
    # A source of only whitespace must be treated as empty (no junk formula node).
    src = _doc({"type": "mathBlock", "attrs": {"text": "   "}})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    assert out["content"] == []
    assert any("empty mathBlock dropped" in w for w in warnings)


def test_math_block_preserves_interior_whitespace_in_source():
    # Non-empty LaTeX with interior spaces is emitted verbatim (un-stripped).
    src = _doc({"type": "mathBlock", "attrs": {"text": "a + b = c"}})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out == {"type": "formula", "attrs": {"source": "a + b = c"}}


def test_math_inline_becomes_inline_formula_between_text():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("before "),
                {"type": "mathInline", "attrs": {"text": "x_i"}},
                _text(" after"),
            ],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [
        {"type": "text", "text": "before "},
        {"type": "inline_formula", "attrs": {"source": "x_i"}},
        {"type": "text", "text": " after"},
    ]


def test_empty_math_inline_skipped_with_warning():
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("a"),
                {"type": "mathInline", "attrs": {"text": ""}},
                _text("b"),
            ],
        }
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert any("empty mathInline dropped" in w for w in warnings)


def test_whitespace_only_math_inline_skipped_with_warning():
    # A whitespace-only inline source is treated as empty (no junk formula node).
    src = _doc(
        {
            "type": "paragraph",
            "content": [
                _text("a"),
                {"type": "mathInline", "attrs": {"text": "   "}},
                _text("b"),
            ],
        }
    )
    warnings: list[str] = []
    inline = docmost_to_habr_doc(src, warnings=warnings)["content"][0]["content"]
    assert inline == [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert any("empty mathInline dropped" in w for w in warnings)


def test_math_inline_preserves_interior_whitespace_in_source():
    # Non-empty inline LaTeX with interior spaces is emitted verbatim (un-stripped).
    src = _doc(
        {
            "type": "paragraph",
            "content": [{"type": "mathInline", "attrs": {"text": "x + y"}}],
        }
    )
    inline = docmost_to_habr_doc(src)["content"][0]["content"]
    assert inline == [{"type": "inline_formula", "attrs": {"source": "x + y"}}]


# --- embed / youtube ---------------------------------------------------------


def test_embed_becomes_embed_node():
    src = _doc({"type": "embed", "attrs": {"src": "https://youtu.be/x"}})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out == {
        "type": "embed",
        "attrs": {"src": "https://youtu.be/x", "inserted": False},
    }


def test_youtube_becomes_embed_node():
    src = _doc({"type": "youtube", "attrs": {"src": "https://youtu.be/y"}})
    out = docmost_to_habr_doc(src)["content"][0]
    assert out == {
        "type": "embed",
        "attrs": {"src": "https://youtu.be/y", "inserted": False},
    }


def test_embed_without_src_dropped_with_warning():
    src = _doc({"type": "embed", "attrs": {}})
    warnings: list[str] = []
    out = docmost_to_habr_doc(src, warnings=warnings)
    assert out["content"] == []
    assert any("embed dropped (no src)" in w for w in warnings)


# --- no Docmost node names leak into the output ------------------------------


def test_converted_doc_never_contains_docmost_node_names():
    src = _doc(
        {
            "type": "table",
            "content": [
                {
                    "type": "tableRow",
                    "content": [_table_cell("tableCell", "c")],
                }
            ],
        },
        {"type": "mathBlock", "attrs": {"text": "e=mc^2"}},
        {
            "type": "paragraph",
            "content": [{"type": "mathInline", "attrs": {"text": "x"}}],
        },
        {"type": "youtube", "attrs": {"src": "https://youtu.be/z"}},
    )
    source = serialize_source(docmost_to_habr_doc(src))
    # Docmost-only node names that must never appear as an output "type" token.
    # NB: "table" is intentionally excluded — Habr's own valid output nests a
    # {"type":"table"} node inside table_wrapper, so the bare word collides.
    # tableRow/tableCell collide with no Habr name (Habr uses table_row/cell).
    for docmost_name in (
        "tableRow",
        "tableCell",
        "mathBlock",
        "mathInline",
        "youtube",
    ):
        # Match the JSON type token exactly so "table_row"/"table_paragraph" etc.
        # do not trip the check.
        assert f'"type":"{docmost_name}"' not in source
    # The output must use Habr's snake_case table nodes, not Docmost camelCase.
    assert '"type":"table_wrapper"' in source
    assert '"type":"table_row"' in source
    assert '"type":"table_cell"' in source


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
#
# The announce is a SEPARATE, caller-supplied field — never derived from the
# article body. ``make_preview_doc`` takes only that text, strips it, hard-caps
# it at 3000 chars on a word boundary and wraps it in one inline paragraph.


def test_make_preview_doc_single_paragraph_no_align():
    # A teaser >= 100 chars becomes the text of exactly one inline paragraph; the
    # paragraph carries the canonical attrs and nothing else.
    announce = (
        "Это рукописный анонс до ката, который автор пишет сам и который должен "
        "быть достаточно длинным, чтобы пройти нижнюю границу в сто символов."
    )
    assert len(announce) >= 100
    preview = make_preview_doc(announce)
    assert preview["type"] == "doc"
    assert len(preview["content"]) == 1
    para = preview["content"][0]
    assert para["type"] == "paragraph"
    assert para["attrs"] == {"simple": False, "persona": False}
    assert para["content"][0]["text"] == announce


def test_make_preview_doc_strips_surrounding_whitespace():
    preview = make_preview_doc("  Явный анонс.  ")
    assert preview["content"][0]["content"][0]["text"] == "Явный анонс."


def test_make_preview_doc_empty_announce_yields_empty_paragraph():
    # An empty announce still emits one paragraph (the client gates length before
    # ever calling this, so make_preview_doc itself does not raise).
    preview = make_preview_doc("")
    assert len(preview["content"]) == 1
    assert preview["content"][0]["content"][0]["text"] == ""


def test_make_preview_doc_caps_at_3000_on_word_boundary():
    word = "слово "
    long_announce = word * 700  # ~4200 chars
    preview = make_preview_doc(long_announce)
    text = preview["content"][0]["content"][0]["text"]
    assert len(text) <= 3000
    # Trimmed on a word boundary: no trailing partial word / no dangling space.
    assert not text.endswith(" ")
    assert text.endswith("слово")


def test_make_preview_doc_cap_keeps_most_when_only_early_space():
    # A long announce whose ONLY space is near the start must not collapse to a
    # few chars: the early word boundary is ignored and the hard 3000-char cut
    # wins.
    announce = "См " + "x" * 3500  # single space at index 2
    preview = make_preview_doc(announce)
    text = preview["content"][0]["content"][0]["text"]
    assert len(text) <= 3000
    assert len(text) >= 1500  # not collapsed to "См"
