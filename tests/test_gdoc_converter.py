"""Unit tests for the pure Google Docs -> Docmost (TipTap) converter.

The converter emits an *intermediate* Docmost-shaped doc that the existing
``src.converter`` pipeline understands. Tests assert the intermediate shapes
directly; one end-to-end test feeds the result through ``docmost_to_habr_doc`` to
prove the two converters compose.
"""

from __future__ import annotations

import json

import pytest

from src.converter import docmost_to_habr_doc
from src.gdoc_converter import gdoc_to_docmost_doc


# --- builders ----------------------------------------------------------------


def _run(content: str, style: dict | None = None) -> dict:
    """A textRun ParagraphElement."""
    return {"textRun": {"content": content, "textStyle": style or {}}}


def _para(*elements: dict, style: dict | None = None, bullet: dict | None = None,
          positioned: list[str] | None = None) -> dict:
    """A paragraph StructuralElement."""
    paragraph: dict = {"elements": list(elements)}
    if style is not None:
        paragraph["paragraphStyle"] = style
    if bullet is not None:
        paragraph["bullet"] = bullet
    if positioned is not None:
        paragraph["positionedObjectIds"] = positioned
    return {"paragraph": paragraph}


def _gdoc(*structural: dict, **top: dict) -> dict:
    """Wrap structural elements in a legacy (no-tabs) Document body."""
    doc: dict = {"body": {"content": list(structural)}}
    doc.update(top)
    return doc


def _blocks(*structural: dict, **top: dict) -> list[dict]:
    """Convert and return the doc's top-level content blocks."""
    return gdoc_to_docmost_doc(_gdoc(*structural, **top))["content"]


# --- input normalization -----------------------------------------------------


def test_accepts_plain_document_dict():
    out = gdoc_to_docmost_doc(_gdoc(_para(_run("hi\n"))))
    assert out == {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}],
    }


def test_accepts_json_string():
    out = gdoc_to_docmost_doc(json.dumps(_gdoc(_para(_run("hi\n")))))
    assert out["content"][0]["content"][0]["text"] == "hi"


def test_accepts_wrapper_under_document_key():
    wrapped = {"document": _gdoc(_para(_run("hi\n")))}
    out = gdoc_to_docmost_doc(wrapped)
    assert out["content"][0]["content"][0]["text"] == "hi"


def test_tabs_only_document_is_recognised():
    # A Document with only `tabs` (no `body`) is still a valid Document.
    doc = {"tabs": []}
    assert gdoc_to_docmost_doc(doc) == {"type": "doc", "content": []}


def test_rejects_docmost_doc():
    with pytest.raises(ValueError, match="not a Google Docs document"):
        gdoc_to_docmost_doc({"type": "doc", "content": []})


def test_rejects_non_document_dict():
    with pytest.raises(ValueError, match="not a Google Docs document"):
        gdoc_to_docmost_doc({"foo": "bar"})


def test_rejects_non_dict():
    with pytest.raises(ValueError, match="not a Google Docs document"):
        gdoc_to_docmost_doc(123)


# --- paragraphs & headings ---------------------------------------------------


def test_plain_paragraph():
    blocks = _blocks(_para(_run("Hello world\n")))
    assert blocks == [
        {"type": "paragraph", "content": [{"type": "text", "text": "Hello world"}]}
    ]


def test_heading_levels():
    for named, level in (("HEADING_1", 1), ("HEADING_3", 3), ("HEADING_6", 6)):
        blocks = _blocks(_para(_run("H\n"), style={"namedStyleType": named}))
        assert blocks[0]["type"] == "heading"
        assert blocks[0]["attrs"]["level"] == level


def test_title_dropped_subtitle_maps_to_level_2():
    # A Google Docs TITLE is the document title; in Habr the post title is a
    # separate field, so a TITLE paragraph is dropped from the body (with a once
    # warning) instead of being emitted as a heading that duplicates the title.
    warnings: list[str] = []
    title_blocks = gdoc_to_docmost_doc(
        _gdoc(_para(_run("T\n"), style={"namedStyleType": "TITLE"})), warnings
    )["content"]
    assert title_blocks == []
    assert any("TITLE" in w for w in warnings)
    subtitle = _blocks(_para(_run("S\n"), style={"namedStyleType": "SUBTITLE"}))[0]
    assert subtitle["type"] == "heading" and subtitle["attrs"]["level"] == 2


def test_leading_title_not_duplicated_in_body():
    # Regression: a leading TITLE (typically identical to the article title) must
    # not be emitted as an in-body heading and duplicate the Habr post title.
    blocks = _blocks(
        _para(_run("My Article\n"), style={"namedStyleType": "TITLE"}),
        _para(_run("Intro paragraph\n"), style={"namedStyleType": "NORMAL_TEXT"}),
    )
    assert [b["type"] for b in blocks] == ["paragraph"]
    assert blocks[0]["content"][0]["text"] == "Intro paragraph"


def test_normal_text_is_paragraph():
    blocks = _blocks(_para(_run("x\n"), style={"namedStyleType": "NORMAL_TEXT"}))
    assert blocks[0]["type"] == "paragraph"


def test_alignment_mapping():
    cases = {
        "CENTER": "center",
        "END": "right",
        "JUSTIFIED": "justify",
    }
    for alignment, expected in cases.items():
        blocks = _blocks(_para(_run("x\n"), style={"alignment": alignment}))
        assert blocks[0]["attrs"]["textAlign"] == expected


def test_alignment_start_and_unspecified_omit_attr():
    for alignment in ("START", "ALIGNMENT_UNSPECIFIED"):
        blocks = _blocks(_para(_run("x\n"), style={"alignment": alignment}))
        assert "attrs" not in blocks[0]


def test_empty_paragraph_emits_blank_node():
    # A paragraph whose only content is the terminating newline -> empty node.
    blocks = _blocks(_para(_run("\n")))
    assert blocks == [{"type": "paragraph"}]


def test_empty_paragraph_with_no_elements():
    blocks = _blocks({"paragraph": {"elements": []}})
    assert blocks == [{"type": "paragraph"}]


# --- marks -------------------------------------------------------------------


def test_basic_marks():
    style = {"bold": True, "italic": True, "underline": True, "strikethrough": True}
    node = _blocks(_para(_run("x\n", style)))[0]["content"][0]
    types = {m["type"] for m in node["marks"]}
    assert types == {"bold", "italic", "underline", "strike"}


def test_superscript_and_subscript():
    sup = _blocks(_para(_run("x\n", {"baselineOffset": "SUPERSCRIPT"})))[0]
    sub = _blocks(_para(_run("y\n", {"baselineOffset": "SUBSCRIPT"})))[0]
    assert sup["content"][0]["marks"] == [{"type": "superscript"}]
    assert sub["content"][0]["marks"] == [{"type": "subscript"}]


def test_baseline_none_has_no_mark():
    node = _blocks(_para(_run("x\n", {"baselineOffset": "NONE"})))[0]["content"][0]
    assert "marks" not in node


def test_external_link_mark():
    node = _blocks(_para(_run("x\n", {"link": {"url": "https://h.com"}})))[0]["content"][0]
    assert node["marks"] == [{"type": "link", "attrs": {"href": "https://h.com"}}]


def test_internal_link_keeps_text_only():
    # headingId/bookmarkId/heading/bookmark/tabId are internal anchors: no href.
    for key in ("headingId", "bookmarkId", "tabId"):
        node = _blocks(_para(_run("x\n", {"link": {key: "abc"}})))[0]["content"][0]
        assert "marks" not in node
        assert node["text"] == "x"


def test_marks_omitted_when_empty():
    node = _blocks(_para(_run("plain\n")))[0]["content"][0]
    assert "marks" not in node


# --- hard breaks, trailing newline, sentinel ---------------------------------


def test_interior_newline_becomes_hard_break():
    blocks = _blocks(_para(_run("a\nb\n")))
    assert blocks[0]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "hardBreak"},
        {"type": "text", "text": "b"},
    ]


def test_soft_break_vertical_tab_becomes_hard_break():
    blocks = _blocks(_para(_run("a\vb\n")))
    assert blocks[0]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "hardBreak"},
        {"type": "text", "text": "b"},
    ]


def test_only_one_trailing_newline_stripped():
    # Two trailing newlines -> the last is the paragraph terminator (stripped),
    # the first becomes a trailing hard break with no text after it.
    blocks = _blocks(_para(_run("a\n\n")))
    assert blocks[0]["content"] == [
        {"type": "text", "text": "a"},
        {"type": "hardBreak"},
    ]


def test_inline_object_sentinel_stripped():
    blocks = _blocks(_para(_run("ab\n")))
    assert blocks[0]["content"] == [{"type": "text", "text": "ab"}]


# --- lists -------------------------------------------------------------------


def _ordered_list(*level_glyphs: str) -> dict:
    """Build a `lists` entry whose nestingLevels carry the given glyphTypes."""
    return {
        "L": {
            "listProperties": {
                "nestingLevels": [{"glyphType": g} for g in level_glyphs]
            }
        }
    }


def test_unordered_list_single_level():
    blocks = _blocks(
        _para(_run("a\n"), bullet={"listId": "L"}),
        _para(_run("b\n"), bullet={"listId": "L"}),
        lists={"L": {"listProperties": {"nestingLevels": [{"glyphType": "NONE"}]}}},
    )
    assert len(blocks) == 1
    assert blocks[0]["type"] == "bulletList"
    assert len(blocks[0]["content"]) == 2
    item = blocks[0]["content"][0]
    assert item["type"] == "listItem"
    assert item["content"][0] == {
        "type": "paragraph",
        "content": [{"type": "text", "text": "a"}],
    }


def test_ordered_list_from_decimal_glyph():
    blocks = _blocks(
        _para(_run("a\n"), bullet={"listId": "L"}),
        lists=_ordered_list("DECIMAL"),
    )
    assert blocks[0]["type"] == "orderedList"


def test_glyph_symbol_is_unordered():
    blocks = _blocks(
        _para(_run("a\n"), bullet={"listId": "L"}),
        lists={"L": {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}]}}},
    )
    assert blocks[0]["type"] == "bulletList"


def test_unresolvable_list_defaults_unordered():
    blocks = _blocks(_para(_run("a\n"), bullet={"listId": "missing"}))
    assert blocks[0]["type"] == "bulletList"


def test_nested_list_nests_inside_parent_item():
    blocks = _blocks(
        _para(_run("outer\n"), bullet={"listId": "L", "nestingLevel": 0}),
        _para(_run("inner\n"), bullet={"listId": "L", "nestingLevel": 1}),
        _para(_run("outer2\n"), bullet={"listId": "L", "nestingLevel": 0}),
        lists=_ordered_list("DECIMAL", "DECIMAL"),
    )
    assert len(blocks) == 1
    top = blocks[0]
    assert top["type"] == "orderedList"
    # The nested list lives INSIDE the first item, after its paragraph.
    first_item = top["content"][0]
    assert first_item["content"][0]["content"][0]["text"] == "outer"
    nested = first_item["content"][1]
    assert nested["type"] == "orderedList"
    assert nested["content"][0]["content"][0]["content"][0]["text"] == "inner"
    # The second outer item is a sibling of the first (same top list).
    assert top["content"][1]["content"][0]["content"][0]["text"] == "outer2"


def test_list_type_switch_at_same_level_starts_new_sibling_list():
    # Level 0 starts ordered, then a level-0 item resolves unordered -> two lists.
    lists = {
        "O": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
        "U": {"listProperties": {"nestingLevels": [{"glyphType": "NONE"}]}},
    }
    blocks = _blocks(
        _para(_run("one\n"), bullet={"listId": "O", "nestingLevel": 0}),
        _para(_run("two\n"), bullet={"listId": "U", "nestingLevel": 0}),
        lists=lists,
    )
    assert [b["type"] for b in blocks] == ["orderedList", "bulletList"]
    assert blocks[0]["content"][0]["content"][0]["content"][0]["text"] == "one"
    assert blocks[1]["content"][0]["content"][0]["content"][0]["text"] == "two"


def test_list_run_is_bounded_by_non_list_paragraphs():
    blocks = _blocks(
        _para(_run("intro\n")),
        _para(_run("a\n"), bullet={"listId": "L"}),
        _para(_run("outro\n")),
        lists=_ordered_list("DECIMAL"),
    )
    assert [b["type"] for b in blocks] == ["paragraph", "orderedList", "paragraph"]


# --- tables ------------------------------------------------------------------


def _cell(text: str, style: dict | None = None) -> dict:
    cell: dict = {"content": [_para(_run(text + "\n"))]}
    if style is not None:
        cell["tableCellStyle"] = style
    return cell


def test_simple_table():
    table = {
        "table": {
            "tableRows": [
                {"tableCells": [_cell("a"), _cell("b")]},
            ]
        }
    }
    blocks = _blocks(table)
    assert blocks[0]["type"] == "table"
    row = blocks[0]["content"][0]
    assert row["type"] == "tableRow"
    assert [c["type"] for c in row["content"]] == ["tableCell", "tableCell"]
    assert row["content"][0]["attrs"] == {"colspan": 1, "rowspan": 1, "colwidth": None}
    assert row["content"][0]["content"][0]["content"][0]["text"] == "a"


def test_header_row_uses_table_header_cells():
    table = {
        "table": {
            "tableRows": [
                {"tableRowStyle": {"tableHeader": True}, "tableCells": [_cell("H")]},
                {"tableCells": [_cell("d")]},
            ]
        }
    }
    blocks = _blocks(table)
    rows = blocks[0]["content"]
    assert rows[0]["content"][0]["type"] == "tableHeader"
    assert rows[1]["content"][0]["type"] == "tableCell"


def test_colspan_and_rowspan():
    table = {
        "table": {
            "tableRows": [
                {"tableCells": [_cell("a", {"columnSpan": 2, "rowSpan": 3})]},
            ]
        }
    }
    attrs = _blocks(table)[0]["content"][0]["content"][0]["attrs"]
    assert attrs["colspan"] == 2
    assert attrs["rowspan"] == 3


def test_recursive_cell_content():
    # A cell holding a list + heading should recurse with the block converter.
    cell = {
        "content": [
            _para(_run("Title\n"), style={"namedStyleType": "HEADING_2"}),
            _para(_run("li\n"), bullet={"listId": "L"}),
        ]
    }
    table = {"table": {"tableRows": [{"tableCells": [cell]}]}}
    blocks = _blocks(table, lists=_ordered_list("DECIMAL"))
    cell_blocks = blocks[0]["content"][0]["content"][0]["content"]
    assert cell_blocks[0]["type"] == "heading"
    assert cell_blocks[1]["type"] == "orderedList"


def test_empty_table_dropped_with_warning():
    warnings: list[str] = []
    out = gdoc_to_docmost_doc(
        _gdoc({"table": {"tableRows": []}}), warnings
    )
    assert out["content"] == []
    assert any("empty table" in w for w in warnings)


# --- inline & positioned images ----------------------------------------------


def _inline_objects(uri: str | None = "https://lh3.googleusercontent.com/img",
                    **extra: object) -> dict:
    embedded: dict = {"title": "T", "description": "D"}
    if uri is not None:
        embedded["imageProperties"] = {"contentUri": uri}
    embedded.update(extra)
    return {"IO": {"inlineObjectProperties": {"embeddedObject": embedded}}}


def test_inline_image_hoisted_with_size_and_alt():
    blocks = _blocks(
        _para({"inlineObjectElement": {"inlineObjectId": "IO"}}),
        inlineObjects={
            "IO": {
                "inlineObjectProperties": {
                    "embeddedObject": {
                        "imageProperties": {"contentUri": "https://x/img"},
                        "title": "Title",
                        "description": "Alt text",
                        "size": {
                            "width": {"magnitude": 72},
                            "height": {"magnitude": 144},
                        },
                    }
                }
            }
        },
    )
    assert blocks == [
        {
            "type": "image",
            "attrs": {
                "src": "https://x/img",
                "alt": "Alt text",
                "title": "Title",
                "width": 96,   # 72 PT * 96/72
                "height": 192,  # 144 PT * 96/72
            },
        }
    ]


def test_paragraph_with_only_image_emits_no_empty_paragraph():
    blocks = _blocks(
        _para({"inlineObjectElement": {"inlineObjectId": "IO"}}),
        inlineObjects=_inline_objects(),
    )
    assert len(blocks) == 1
    assert blocks[0]["type"] == "image"


def test_inline_image_mixed_with_text_splits_blocks():
    blocks = _blocks(
        _para(
            _run("before "),
            {"inlineObjectElement": {"inlineObjectId": "IO"}},
            _run(" after\n"),
        ),
        inlineObjects=_inline_objects(),
    )
    assert [b["type"] for b in blocks] == ["paragraph", "image", "paragraph"]
    assert blocks[0]["content"][0]["text"] == "before "
    assert blocks[2]["content"][0]["text"] == " after"


def test_trailing_newline_stripped_when_image_follows_text_run():
    # The terminating "\n" lives on the text run, but an inline image element
    # follows it. The newline must still be stripped (no spurious hardBreak) and
    # the image must be hoisted into its own block after the text paragraph.
    blocks = _blocks(
        _para(
            _run("text\n"),
            {"inlineObjectElement": {"inlineObjectId": "IO"}},
        ),
        inlineObjects=_inline_objects(),
    )
    assert [b["type"] for b in blocks] == ["paragraph", "image"]
    # No trailing hardBreak: the paragraph holds only the text node.
    assert blocks[0]["content"] == [{"type": "text", "text": "text"}]


def test_drawing_without_content_uri_dropped_with_warning():
    warnings: list[str] = []
    doc = _gdoc(
        _para({"inlineObjectElement": {"inlineObjectId": "IO"}}),
        inlineObjects={
            "IO": {
                "inlineObjectProperties": {
                    "embeddedObject": {"embeddedDrawingProperties": {}}
                }
            }
        },
    )
    out = gdoc_to_docmost_doc(doc, warnings)
    assert out["content"] == []
    assert any("drawing" in w.lower() for w in warnings)


def test_image_missing_content_uri_dropped():
    warnings: list[str] = []
    doc = _gdoc(
        _para({"inlineObjectElement": {"inlineObjectId": "IO"}}),
        inlineObjects={
            "IO": {
                "inlineObjectProperties": {
                    # imageProperties present but with an empty contentUri.
                    "embeddedObject": {"imageProperties": {"contentUri": ""}}
                }
            }
        },
    )
    out = gdoc_to_docmost_doc(doc, warnings)
    assert out["content"] == []
    assert any("contentUri" in w for w in warnings)


def test_positioned_image_appended_after_paragraph():
    blocks = _blocks(
        _para(_run("text\n"), positioned=["PO"]),
        positionedObjects={
            "PO": {
                "positionedObjectProperties": {
                    "embeddedObject": {
                        "imageProperties": {"contentUri": "https://x/pos"},
                        "title": "P",
                        "description": "PD",
                    }
                }
            }
        },
    )
    assert [b["type"] for b in blocks] == ["paragraph", "image"]
    assert blocks[1]["attrs"]["src"] == "https://x/pos"


# --- horizontal rule ---------------------------------------------------------


def test_horizontal_rule_hoisted():
    blocks = _blocks(
        _para(_run("before\n")),
        _para({"horizontalRule": {}}),
        _para(_run("after\n")),
    )
    assert [b["type"] for b in blocks] == [
        "paragraph",
        "horizontalRule",
        "paragraph",
    ]


def test_horizontal_rule_inline_with_text_splits():
    blocks = _blocks(_para(_run("a "), {"horizontalRule": {}}, _run(" b\n")))
    assert [b["type"] for b in blocks] == ["paragraph", "horizontalRule", "paragraph"]


# --- code heuristic ----------------------------------------------------------


def _mono(content: str) -> dict:
    return _run(content, {"weightedFontFamily": {"fontFamily": "Consolas"}})


def test_monospace_paragraph_run_becomes_code_block():
    warnings: list[str] = []
    out = gdoc_to_docmost_doc(
        _gdoc(_para(_mono("line1\n")), _para(_mono("line2\n"))), warnings
    )
    assert out["content"] == [
        {
            "type": "codeBlock",
            "attrs": {"language": None},
            "content": [{"type": "text", "text": "line1\nline2"}],
        }
    ]
    assert any("code block" in w for w in warnings)


def test_code_block_run_broken_by_normal_paragraph():
    blocks = _blocks(
        _para(_mono("code\n")),
        _para(_run("normal\n")),
        _para(_mono("more\n")),
    )
    assert [b["type"] for b in blocks] == ["codeBlock", "paragraph", "codeBlock"]


def test_inline_code_mark_inside_normal_paragraph():
    # A monospace run inside an otherwise-normal paragraph -> inline code mark.
    blocks = _blocks(_para(_run("use "), _mono("printf"), _run(" here\n")))
    assert blocks[0]["type"] == "paragraph"
    nodes = blocks[0]["content"]
    assert nodes[0] == {"type": "text", "text": "use "}
    assert nodes[1] == {"type": "text", "text": "printf", "marks": [{"type": "code"}]}
    assert nodes[2] == {"type": "text", "text": " here"}


def test_monospace_font_detection_case_insensitive_and_variants():
    for font in ("JetBrains Mono", "courier new", "DejaVu Sans Mono"):
        out = gdoc_to_docmost_doc(
            _gdoc(_para(_run("x\n", {"weightedFontFamily": {"fontFamily": font}})))
        )
        assert out["content"][0]["type"] == "codeBlock"


def test_heading_in_monospace_is_not_code_block():
    blocks = _blocks(
        _para(_mono("H\n"), style={"namedStyleType": "HEADING_1"})
    )
    assert blocks[0]["type"] == "heading"


def test_monospace_paragraph_with_smart_chip_is_not_code_block():
    # A monospace paragraph that also carries a smart chip (person) must NOT be
    # swallowed by the code-block collector (which would drop the chip text).
    # It goes through the normal path: the monospace run keeps an inline `code`
    # mark and the chip's name is preserved as plain text.
    blocks = _blocks(
        _para(
            _mono("code "),
            {"person": {"personProperties": {"name": "Alice"}}},
            _run("\n"),
        )
    )
    assert len(blocks) == 1
    assert blocks[0]["type"] == "paragraph"
    nodes = blocks[0]["content"]
    assert nodes[0] == {"type": "text", "text": "code ", "marks": [{"type": "code"}]}
    assert {"type": "text", "text": "Alice"} in nodes


# --- person / richLink / footnote / equation --------------------------------


def test_person_becomes_plain_text_name():
    blocks = _blocks(
        _para({"person": {"personProperties": {"name": "Alice", "email": "a@x"}}}, _run("\n"))
    )
    assert blocks[0]["content"][0] == {"type": "text", "text": "Alice"}


def test_person_falls_back_to_email():
    blocks = _blocks(
        _para({"person": {"personProperties": {"email": "a@x"}}}, _run("\n"))
    )
    assert blocks[0]["content"][0]["text"] == "a@x"


def test_rich_link_becomes_linked_text():
    blocks = _blocks(
        _para(
            {"richLink": {"richLinkProperties": {"title": "Doc", "uri": "https://d"}}},
            _run("\n"),
        )
    )
    node = blocks[0]["content"][0]
    assert node["text"] == "Doc"
    assert node["marks"] == [{"type": "link", "attrs": {"href": "https://d"}}]


def test_rich_link_uses_uri_when_no_title():
    blocks = _blocks(
        _para({"richLink": {"richLinkProperties": {"uri": "https://d"}}}, _run("\n"))
    )
    assert blocks[0]["content"][0]["text"] == "https://d"


def test_footnote_reference_keeps_number():
    blocks = _blocks(
        _para(_run("text"), {"footnoteReference": {"footnoteNumber": "3"}}, _run("\n"))
    )
    texts = [n["text"] for n in blocks[0]["content"] if n["type"] == "text"]
    assert "3" in texts


def test_equation_dropped_with_once_warning():
    warnings: list[str] = []
    gdoc_to_docmost_doc(
        _gdoc(
            _para(_run("a"), {"equation": {}}, _run("\n")),
            _para(_run("b"), {"equation": {}}, _run("\n")),
        ),
        warnings,
    )
    assert warnings.count("equation dropped") == 1


# --- structural elements -----------------------------------------------------


def test_section_break_emits_nothing():
    blocks = _blocks(_para(_run("a\n")), {"sectionBreak": {}}, _para(_run("b\n")))
    assert [b["type"] for b in blocks] == ["paragraph", "paragraph"]


def test_table_of_contents_flattened_with_warning():
    warnings: list[str] = []
    toc = {"tableOfContents": {"content": [_para(_run("entry\n"))]}}
    out = gdoc_to_docmost_doc(_gdoc(toc), warnings)
    assert out["content"][0]["content"][0]["text"] == "entry"
    assert any("table of contents" in w.lower() for w in warnings)


# --- tabs vs legacy ----------------------------------------------------------


def test_tabs_walk_with_per_tab_scope():
    # Two root tabs, each with its own lists map; childTabs are walked too.
    doc = {
        "tabs": [
            {
                "documentTab": {
                    "body": {
                        "content": [
                            _para(_run("tab1 item\n"), bullet={"listId": "L"}),
                        ]
                    },
                    "lists": {
                        "L": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}
                    },
                },
                "childTabs": [
                    {
                        "documentTab": {
                            "body": {"content": [_para(_run("child\n"))]},
                        }
                    }
                ],
            },
            {
                "documentTab": {
                    "body": {
                        "content": [
                            _para(_run("tab2 item\n"), bullet={"listId": "L"}),
                        ]
                    },
                    # SAME listId, but unordered in this tab's scope.
                    "lists": {
                        "L": {"listProperties": {"nestingLevels": [{"glyphType": "NONE"}]}}
                    },
                },
            },
        ]
    }
    blocks = gdoc_to_docmost_doc(doc)["content"]
    # tab1 ordered list, then its child tab paragraph, then tab2 unordered list.
    assert blocks[0]["type"] == "orderedList"
    assert blocks[1] == {
        "type": "paragraph",
        "content": [{"type": "text", "text": "child"}],
    }
    assert blocks[2]["type"] == "bulletList"


def test_tabs_take_precedence_over_legacy_body():
    # When tabs are present and non-empty, legacy top-level body is ignored.
    doc = {
        "body": {"content": [_para(_run("legacy\n"))]},
        "tabs": [
            {"documentTab": {"body": {"content": [_para(_run("from tab\n"))]}}}
        ],
    }
    blocks = gdoc_to_docmost_doc(doc)["content"]
    assert len(blocks) == 1
    assert blocks[0]["content"][0]["text"] == "from tab"


def test_empty_tabs_falls_back_to_legacy_body():
    doc = {"body": {"content": [_para(_run("legacy\n"))]}, "tabs": []}
    blocks = gdoc_to_docmost_doc(doc)["content"]
    assert blocks[0]["content"][0]["text"] == "legacy"


# --- warnings bookkeeping ----------------------------------------------------


def test_warnings_none_is_noop():
    # No warnings list -> no crash even for content that would warn.
    out = gdoc_to_docmost_doc(_gdoc({"table": {"tableRows": []}}))
    assert out["content"] == []


# --- end-to-end composition with the Habr converter --------------------------


def test_pipeline_composes_with_docmost_to_habr():
    doc = _gdoc(
        _para(_run("Title\n"), style={"namedStyleType": "TITLE"}),
        _para(_run("para with "), _run("bold", {"bold": True}), _run(".\n")),
        _para(_run("item\n"), bullet={"listId": "L"}),
        lists=_ordered_list("DECIMAL"),
    )
    intermediate = gdoc_to_docmost_doc(doc)
    habr = docmost_to_habr_doc(intermediate)
    types = [b["type"] for b in habr["content"]]
    # The leading TITLE is dropped (it maps to the post title), so the body starts
    # at the paragraph.
    assert types == ["paragraph", "ordered_list"]
    # The bold mark survives the full pipeline.
    bold_run = habr["content"][0]["content"][1]
    assert {"type": "bold"} in bold_run["marks"]
    # The list uses the canonical Habr "listitem" naming.
    assert "list_item" not in json.dumps(habr)
