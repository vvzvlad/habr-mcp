"""Pure converter: Google Docs API "Document" JSON -> Docmost (TipTap) doc.

The ``google-docs`` MCP tool ``readDocument(format='json')`` returns the native
Google Docs API ``documents.get`` "Document" resource. Rather than translate it
straight to Habr's editorVersion-2 schema, we convert it to an *intermediate*
Docmost-shaped (TipTap/ProseMirror) document and then reuse the battle-tested
``src.converter`` pipeline (``docmost_to_habr_doc`` and friends). That means
images, marks, tables, lists, mentions and previews all flow through code that
already works for the Docmost author path.

Everything here is pure: no network, no file I/O. The HTTP/author layer (image
upload, save, etc.) lives elsewhere and calls ``gdoc_to_docmost_doc`` first.

Output is restricted to the node/mark types ``src.converter`` understands, so the
result degrades gracefully:
- block: ``paragraph`` (optional ``attrs.textAlign``), ``heading``,
  ``codeBlock``, ``horizontalRule``, ``bulletList``/``orderedList``/``listItem``,
  ``image``, ``table`` (``tableRow``/``tableCell``/``tableHeader``);
- inline: ``text`` (+ marks ``bold``/``italic``/``underline``/``strike``/
  ``subscript``/``superscript``/``code``/``link``), ``hardBreak``.

Design notes:
- Warnings are appended to the caller-provided ``warnings`` list. Some warnings
  are de-duplicated ("once" warnings) so a long document does not spam them.
- Google Docs indices (``startIndex``/``endIndex``) are UTF-16 and end-exclusive;
  we never rely on them, walking the structural tree directly instead.
"""

from __future__ import annotations

import json
from typing import Any

# --- Constants ---------------------------------------------------------------

# A Google Docs textRun replaces an inline non-text element (e.g. an inline
# image or a smart chip rendered as text) with this private-use sentinel inside
# the run's content; we strip it so it does not leak into the output.
_INLINE_OBJECT_SENTINEL = ""

# Soft line break (Shift+Enter inside a paragraph) shows up as a vertical tab.
_SOFT_BREAK = "\v"

# namedStyleType values that map to a heading. SUBTITLE maps to level 2; the Habr
# converter clamps everything into 1..3 anyway. TITLE is handled separately (see
# ``_TITLE_STYLE``) and is NOT a heading.
_HEADING_LEVELS = {
    "HEADING_1": 1,
    "HEADING_2": 2,
    "HEADING_3": 3,
    "HEADING_4": 4,
    "HEADING_5": 5,
    "HEADING_6": 6,
    "SUBTITLE": 2,
}

# A Google Docs TITLE paragraph is the document's title. In Habr the post title is
# a separate field, so emitting TITLE as an in-body heading would duplicate the
# article title; TITLE paragraphs are dropped from the body instead.
_TITLE_STYLE = "TITLE"

# paragraphStyle.alignment -> Docmost paragraph attrs.textAlign. START and the
# unspecified value are omitted entirely (no textAlign key).
_ALIGNMENT_MAP = {
    "CENTER": "center",
    "END": "right",
    "JUSTIFIED": "justify",
}

# glyphType values that make a list ORDERED. Anything else (NONE, unspecified,
# absent, or a glyphSymbol) is unordered.
_ORDERED_GLYPH_TYPES = {
    "DECIMAL",
    "ZERO_DECIMAL",
    "UPPER_ALPHA",
    "ALPHA",
    "UPPER_ROMAN",
    "ROMAN",
}

# Monospace font families (case-insensitive substring match) used by the
# code-block / inline-code heuristic. Google Docs has no code concept, so a run
# typed in one of these fonts is treated as code.
_MONOSPACE_FONTS = (
    "consolas",
    "courier new",
    "courier",
    "roboto mono",
    "source code pro",
    "inconsolata",
    "menlo",
    "monaco",
    "fira mono",
    "fira code",
    "jetbrains mono",
    "ibm plex mono",
    "ubuntu mono",
    "cousine",
    "pt mono",
    "space mono",
    "dejavu sans mono",
)

# PT_PER_PX is 0.75 (96 dpi); width/height magnitudes in the embedded object are
# in PT, but Docmost/Habr image attrs are integer pixels. We round to int px.
_PT_TO_PX = 96.0 / 72.0


# --- Input normalization -----------------------------------------------------


def _as_gdoc(value: Any) -> dict:
    """Return the Google Docs "Document" dict from a flexible input.

    Accepts (a) the Document dict itself, (b) a JSON string of it, or (c) an
    object wrapping the Document under a ``document``/``data``/``result`` key. A
    Document is recognised by a ``body`` or ``tabs`` key (and is NOT a Docmost
    ``{"type":"doc"}`` doc). Raises ``ValueError`` otherwise.
    """
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("not a Google Docs document")
    if value.get("type") == "doc":
        # A Docmost doc, not a Google Docs Document.
        raise ValueError("not a Google Docs document")
    if "body" in value or "tabs" in value:
        return value
    # A thin wrapper around the Document (e.g. an MCP envelope).
    for key in ("document", "data", "result"):
        inner = value.get(key)
        if isinstance(inner, dict) and ("body" in inner or "tabs" in inner):
            return inner
    raise ValueError("not a Google Docs document")


# --- Warning bookkeeping -----------------------------------------------------


def _warn(warnings: list[str] | None, message: str) -> None:
    """Append ``message`` to ``warnings`` (no-op if ``warnings`` is None)."""
    if warnings is not None:
        warnings.append(message)


def _warn_once(warnings: list[str] | None, seen: set[str], message: str) -> None:
    """Append ``message`` at most once across the whole conversion."""
    if warnings is None or message in seen:
        return
    seen.add(message)
    warnings.append(message)


# --- Scope (per-tab maps) ----------------------------------------------------


class _Scope:
    """The map context for the structural content currently being walked.

    Google Docs resolves ``lists``/``inlineObjects``/``positionedObjects`` from
    either the top level (legacy docs) or the owning tab (``documentTab.*``).
    Always resolve from the SAME scope you are walking, so each walk carries its
    own ``_Scope``.
    """

    def __init__(
        self,
        lists: dict[str, Any],
        inline_objects: dict[str, Any],
        positioned_objects: dict[str, Any],
    ) -> None:
        self.lists = lists or {}
        self.inline_objects = inline_objects or {}
        self.positioned_objects = positioned_objects or {}


# --- Text style / marks ------------------------------------------------------


def _is_monospace(font_family: Any) -> bool:
    """True if ``font_family`` (a string) looks like a monospace font."""
    if not isinstance(font_family, str):
        return False
    name = font_family.strip().lower()
    if not name:
        return False
    return any(mono in name for mono in _MONOSPACE_FONTS)


def _run_font_family(text_style: dict) -> Any:
    """Return ``weightedFontFamily.fontFamily`` from a textStyle (or None)."""
    wff = text_style.get("weightedFontFamily")
    if isinstance(wff, dict):
        return wff.get("fontFamily")
    return None


def _link_href(text_style: dict) -> str | None:
    """Return the external link URL of a textStyle, or None.

    Only ``link.url`` is an external link. ``link.headingId``/``bookmarkId``/
    ``heading``/``bookmark``/``tabId`` are INTERNAL document anchors with no web
    href, so we drop the link and keep the text only.
    """
    link = text_style.get("link")
    if not isinstance(link, dict):
        return None
    url = link.get("url")
    if isinstance(url, str) and url:
        return url
    return None


def _marks_for_style(text_style: dict, *, as_code: bool) -> list[dict]:
    """Build the Docmost marks for a Google Docs textStyle.

    ``as_code`` forces a ``code`` mark (used when the whole run is monospace but
    the paragraph is NOT a pure code line). A ``link`` mark is appended last so
    the link href survives alongside formatting marks.
    """
    marks: list[dict] = []
    if text_style.get("bold"):
        marks.append({"type": "bold"})
    if text_style.get("italic"):
        marks.append({"type": "italic"})
    if text_style.get("underline"):
        marks.append({"type": "underline"})
    if text_style.get("strikethrough"):
        marks.append({"type": "strike"})
    baseline = text_style.get("baselineOffset")
    if baseline == "SUPERSCRIPT":
        marks.append({"type": "superscript"})
    elif baseline == "SUBSCRIPT":
        marks.append({"type": "subscript"})
    if as_code:
        marks.append({"type": "code"})
    href = _link_href(text_style)
    if href:
        marks.append({"type": "link", "attrs": {"href": href}})
    return marks


# --- Inline image extraction -------------------------------------------------


def _coerce_px(magnitude: Any) -> int | None:
    """Convert a PT magnitude to integer pixels, or None when absent/invalid."""
    try:
        return int(round(float(magnitude) * _PT_TO_PX))
    except (TypeError, ValueError):
        return None


def _embedded_object_to_image(
    embedded: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict | None:
    """Build a Docmost ``image`` block from an embeddedObject, or None.

    Returns None (with a once-warning) for a Drawing (``embeddedDrawingProperties``)
    or any object lacking an image ``contentUri``.
    """
    if not isinstance(embedded, dict):
        return None
    if "embeddedDrawingProperties" in embedded:
        _warn_once(warnings, seen, "google drawing dropped (no image url)")
        return None
    image_props = embedded.get("imageProperties")
    if not isinstance(image_props, dict):
        _warn_once(warnings, seen, "embedded object without image dropped")
        return None
    content_uri = image_props.get("contentUri")
    if not isinstance(content_uri, str) or not content_uri:
        _warn_once(warnings, seen, "image dropped (no contentUri)")
        return None

    size = embedded.get("size")
    width = height = None
    if isinstance(size, dict):
        width = _coerce_px((size.get("width") or {}).get("magnitude"))
        height = _coerce_px((size.get("height") or {}).get("magnitude"))

    return {
        "type": "image",
        "attrs": {
            "src": content_uri,
            "alt": embedded.get("description"),
            "title": embedded.get("title"),
            "width": width,
            "height": height,
        },
    }


def _inline_object_image(
    inline_object_id: Any,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> dict | None:
    """Resolve an inlineObjectElement to a Docmost ``image`` block, or None."""
    obj = scope.inline_objects.get(inline_object_id)
    if not isinstance(obj, dict):
        _warn_once(warnings, seen, "inline image dropped (id not found)")
        return None
    props = obj.get("inlineObjectProperties")
    embedded = props.get("embeddedObject") if isinstance(props, dict) else None
    return _embedded_object_to_image(embedded or {}, warnings, seen)


def _positioned_object_images(
    paragraph: dict,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Best-effort: build image blocks for a paragraph's positionedObjectIds."""
    images: list[dict] = []
    for obj_id in paragraph.get("positionedObjectIds") or []:
        obj = scope.positioned_objects.get(obj_id)
        if not isinstance(obj, dict):
            _warn_once(warnings, seen, "positioned image dropped (id not found)")
            continue
        props = obj.get("positionedObjectProperties")
        embedded = props.get("embeddedObject") if isinstance(props, dict) else None
        image = _embedded_object_to_image(embedded or {}, warnings, seen)
        if image is not None:
            images.append(image)
    return images


# --- Paragraph inline conversion ---------------------------------------------


def _text_node(text: str, marks: list[dict]) -> dict:
    """Build a Docmost text node, omitting ``marks`` when empty."""
    node: dict[str, Any] = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _split_text_run(
    text: str, marks: list[dict], inline: list[dict]
) -> None:
    """Append text run ``text`` (with ``marks``) to ``inline``, splitting breaks.

    Interior newlines and soft breaks become ``hardBreak`` nodes; the sentinel
    for non-text inline elements is stripped. Empty segments never emit a node.
    """
    # Normalise soft breaks to newlines so a single split handles both.
    text = text.replace(_SOFT_BREAK, "\n").replace(_INLINE_OBJECT_SENTINEL, "")
    segments = text.split("\n")
    for index, segment in enumerate(segments):
        if index > 0:
            inline.append({"type": "hardBreak"})
        if segment:
            inline.append(_text_node(segment, marks))


def _convert_text_run(
    element: dict, is_last_text_run: bool, stream: list[Any]
) -> None:
    """Convert one ``textRun`` element into inline nodes appended to ``stream``.

    A monospace run gets the inline ``code`` mark (the pure-code-LINE case never
    reaches here — it is handled by the code-block path). The single trailing
    paragraph newline is stripped on the LAST textRun of the paragraph (not the
    last element): a trailing inline image after the text must not leave a
    spurious hardBreak from the terminator.
    """
    run = element.get("textRun") or {}
    content = run.get("content") or ""
    text_style = run.get("textStyle") or {}
    if is_last_text_run and content.endswith("\n"):
        content = content[:-1]
    as_code = _is_monospace(_run_font_family(text_style))
    marks = _marks_for_style(text_style, as_code=as_code)
    _split_text_run(content, marks, stream)


def _convert_misc_inline(
    element: dict,
    scope: _Scope,
    stream: list[Any],
    warnings: list[str] | None,
    seen: set[str],
) -> None:
    """Convert a non-textRun ParagraphElement into inline/block nodes.

    Handles inline images, horizontal rules, persons, rich links and footnote
    references; layout-only elements (page/column break, autoText, equation) are
    dropped (equation warns once).
    """
    if "inlineObjectElement" in element:
        ioe = element.get("inlineObjectElement") or {}
        image = _inline_object_image(ioe.get("inlineObjectId"), scope, warnings, seen)
        if image is not None:
            stream.append(image)
    elif "horizontalRule" in element:
        stream.append({"type": "horizontalRule"})
    elif "person" in element:
        person = element.get("person") or {}
        props = person.get("personProperties") or {}
        label = props.get("name") or props.get("email") or ""
        if label:
            marks = _marks_for_style(person.get("textStyle") or {}, as_code=False)
            stream.append(_text_node(label, marks))
    elif "richLink" in element:
        rich = element.get("richLink") or {}
        props = rich.get("richLinkProperties") or {}
        uri = props.get("uri") or ""
        label = props.get("title") or uri
        if label:
            marks = [{"type": "link", "attrs": {"href": uri}}] if uri else []
            stream.append(_text_node(label, marks))
    elif "footnoteReference" in element:
        number = (element.get("footnoteReference") or {}).get("footnoteNumber")
        if number:
            stream.append(_text_node(str(number), []))
    elif "equation" in element:
        _warn_once(warnings, seen, "equation dropped")
    # autoText / pageBreak / columnBreak: no body representation, dropped silently.


def _convert_paragraph_elements(
    paragraph: dict,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> list[Any]:
    """Convert a paragraph's elements into an ordered stream of inline + blocks.

    The returned ``stream`` interleaves inline nodes (text/hardBreak) and BLOCK
    nodes (image/horizontalRule) in document order; the caller hoists the block
    nodes out. The trailing ``"\n"`` that ends every paragraph is stripped (one
    newline only).
    """
    elements = paragraph.get("elements") or []
    # Index of the LAST textRun element: the paragraph terminator "\n" lives on
    # that run, even when other (non-textRun) elements follow it in the list.
    last_text_run_index = -1
    for index, element in enumerate(elements):
        if isinstance(element, dict) and "textRun" in element:
            last_text_run_index = index
    stream: list[Any] = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            continue
        if "textRun" in element:
            _convert_text_run(element, index == last_text_run_index, stream)
        else:
            _convert_misc_inline(element, scope, stream, warnings, seen)
    return stream


# --- Block (StructuralElement) conversion ------------------------------------


def _split_stream_to_blocks(
    stream: list[Any],
    align: str | None,
) -> list[dict]:
    """Split an inline+block stream into Docmost block nodes (image hoisting).

    Contiguous inline runs become a ``paragraph`` (carrying ``attrs.textAlign``
    when set); each block node (image/horizontalRule) becomes its own sibling
    block in document order. A paragraph with only inline whitespace is dropped;
    a fully empty stream yields an empty paragraph node.
    """
    blocks: list[dict] = []
    inline_buffer: list[dict] = []

    def flush() -> None:
        if not inline_buffer:
            return
        # Drop a buffer that is only hardBreaks / empty text.
        has_content = any(
            node.get("type") == "text" and node.get("text") for node in inline_buffer
        )
        if has_content:
            para: dict[str, Any] = {"type": "paragraph", "content": list(inline_buffer)}
            if align:
                para["attrs"] = {"textAlign": align}
            blocks.append(para)
        inline_buffer.clear()

    for node in stream:
        ntype = node.get("type")
        if ntype in ("image", "horizontalRule"):
            flush()
            blocks.append(node)
        else:
            inline_buffer.append(node)
    flush()
    return blocks


def _build_paragraph_blocks(
    paragraph: dict,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a single (non-list) paragraph into zero or more Docmost blocks.

    Handles heading vs paragraph, alignment, image/hr hoisting, inline ``code``
    marks (monospace runs), and appends positioned-object images after the
    paragraph. A pure code LINE is NOT handled here; the caller groups those into
    a code block.
    """
    style = paragraph.get("paragraphStyle") or {}
    named = style.get("namedStyleType")
    if named == _TITLE_STYLE:
        # The document TITLE maps to the Habr post title (a separate field), so it
        # must not be duplicated as an in-body heading. Drop the paragraph.
        _warn_once(
            warnings,
            seen,
            "document TITLE dropped from body (it maps to the article title)",
        )
        return []
    alignment = style.get("alignment")
    align = _ALIGNMENT_MAP.get(alignment) if isinstance(alignment, str) else None

    stream = _convert_paragraph_elements(paragraph, scope, warnings, seen)
    heading_level = _HEADING_LEVELS.get(named) if isinstance(named, str) else None

    blocks: list[dict]
    if heading_level is not None:
        # A heading: emit any hoisted images/rules around a heading node.
        blocks = _split_stream_for_heading(stream, heading_level)
    else:
        blocks = _split_stream_to_blocks(stream, align)
        if not blocks and not _has_non_text_element(paragraph):
            # Truly blank line (only textRuns, none with content): emit one empty
            # paragraph node so blank lines are preserved. A paragraph whose only
            # content was an image/rule (now hoisted or dropped) gets NO empty
            # paragraph fallback.
            blocks = [{"type": "paragraph"}]

    blocks.extend(_positioned_object_images(paragraph, scope, warnings, seen))
    return blocks


def _has_non_text_element(paragraph: dict) -> bool:
    """True if the paragraph carries any non-textRun ParagraphElement.

    Used so a paragraph whose only content was an inline image / horizontal rule
    (now hoisted into its own block, or dropped) does not also produce a stray
    empty paragraph node.
    """
    for element in paragraph.get("elements") or []:
        if isinstance(element, dict) and "textRun" not in element:
            return True
    return False


def _split_stream_for_heading(stream: list[Any], level: int) -> list[dict]:
    """Like ``_split_stream_to_blocks`` but inline runs become a ``heading``."""
    blocks: list[dict] = []
    inline_buffer: list[dict] = []

    def flush() -> None:
        has_content = any(
            node.get("type") == "text" and node.get("text") for node in inline_buffer
        )
        if has_content:
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": level},
                    "content": list(inline_buffer),
                }
            )
        inline_buffer.clear()

    for node in stream:
        if node.get("type") in ("image", "horizontalRule"):
            flush()
            blocks.append(node)
        else:
            inline_buffer.append(node)
    flush()
    return blocks


# --- Lists -------------------------------------------------------------------


def _list_is_ordered(scope: _Scope, list_id: Any, nesting_level: int) -> bool:
    """Resolve whether (list_id, nesting_level) is an ordered list.

    Ordered iff the level's ``glyphType`` is an ordered enum. Unresolvable
    levels (missing list / index out of range / glyphSymbol) default to
    unordered, matching the spec.
    """
    lst = scope.lists.get(list_id)
    if not isinstance(lst, dict):
        return False
    props = lst.get("listProperties")
    if not isinstance(props, dict):
        return False
    levels = props.get("nestingLevels")
    if not isinstance(levels, list) or nesting_level >= len(levels):
        return False
    level = levels[nesting_level]
    if not isinstance(level, dict):
        return False
    if "glyphSymbol" in level:
        return False
    glyph_type = level.get("glyphType")
    return glyph_type in _ORDERED_GLYPH_TYPES


def _list_item_paragraph(blocks: list[dict]) -> dict:
    """Return the first paragraph/heading block to seed a listItem, or a blank one."""
    for block in blocks:
        if block.get("type") in ("paragraph", "heading"):
            return block
    return {"type": "paragraph"}


def _build_list(
    items: list[dict],
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Build nested Docmost lists from a run of consecutive list-item paragraphs.

    ``items`` is the maximal run of paragraphs that each carry a ``bullet``. We
    use a stack keyed by nestingLevel: a deeper item nests a new list inside the
    previous item; a shallower item pops back. When the ordered-ness changes at
    the same level, a new sibling list starts.

    Returns the top-level list nodes produced by the run.
    """
    top: list[dict] = []
    # Stack frames: (level, ordered, list_node, current_item_node).
    stack: list[dict[str, Any]] = []

    for paragraph in items:
        bullet = paragraph.get("bullet") or {}
        list_id = bullet.get("listId")
        level = bullet.get("nestingLevel") or 0
        ordered = _list_is_ordered(scope, list_id, level)

        # Convert the item's content into Docmost blocks; the first paragraph/
        # heading seeds the listItem, anything extra (images) is appended.
        item_blocks = _build_paragraph_blocks(paragraph, scope, warnings, seen)
        # A list item must not carry bullet/heading semantics on its paragraph,
        # so strip any align-only attrs is fine; headings inside a list item are
        # unusual but allowed by the Habr converter (flattened later).
        first_block = _list_item_paragraph(item_blocks)
        extra_blocks = [b for b in item_blocks if b is not first_block]
        list_item: dict[str, Any] = {
            "type": "listItem",
            "content": [first_block, *extra_blocks],
        }

        # Pop frames deeper than this level.
        while stack and stack[-1]["level"] > level:
            stack.pop()

        if stack and stack[-1]["level"] == level:
            frame = stack[-1]
            if frame["ordered"] != ordered:
                # Ordered-ness switched at this level: start a new sibling list in
                # the same parent (top-level or inside the parent item).
                new_list = _new_list_node(ordered)
                parent_item = stack[-2]["item"] if len(stack) >= 2 else None
                if parent_item is not None:
                    parent_item["content"].append(new_list)
                else:
                    top.append(new_list)
                frame["list"] = new_list
                frame["ordered"] = ordered
            frame["list"]["content"].append(list_item)
            frame["item"] = list_item
        else:
            # New deeper level (or the very first item): create a list node and
            # attach it to the parent item (if any) or to the top level.
            new_list = _new_list_node(ordered)
            if stack:
                stack[-1]["item"]["content"].append(new_list)
            else:
                top.append(new_list)
            new_list["content"].append(list_item)
            stack.append(
                {
                    "level": level,
                    "ordered": ordered,
                    "list": new_list,
                    "item": list_item,
                }
            )

    return top


def _new_list_node(ordered: bool) -> dict:
    """Create an empty Docmost ordered/bullet list node."""
    return {"type": "orderedList" if ordered else "bulletList", "content": []}


# --- Structural element walking ----------------------------------------------


def _convert_structural_content(
    content: Any,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a list of StructuralElements into Docmost block nodes.

    Groups consecutive list-item paragraphs into nested lists and consecutive
    pure-code-line paragraphs into a single ``codeBlock``. Tables, tableOfContents
    (flattened), and sectionBreaks (skipped) are handled too.
    """
    blocks: list[dict] = []
    if not isinstance(content, list):
        return blocks

    index = 0
    while index < len(content):
        element = content[index]
        if not isinstance(element, dict):
            index += 1
            continue

        paragraph = element.get("paragraph")
        if isinstance(paragraph, dict):
            if paragraph.get("bullet"):
                # Collect the maximal run of consecutive list-item paragraphs.
                run: list[dict] = []
                while index < len(content):
                    nxt = content[index]
                    nxt_para = nxt.get("paragraph") if isinstance(nxt, dict) else None
                    if isinstance(nxt_para, dict) and nxt_para.get("bullet"):
                        run.append(nxt_para)
                        index += 1
                    else:
                        break
                blocks.extend(_build_list(run, scope, warnings, seen))
                continue

            if _is_code_line(paragraph):
                # Collect a maximal run of consecutive pure-code-line paragraphs.
                lines: list[str] = []
                while index < len(content):
                    nxt = content[index]
                    nxt_para = nxt.get("paragraph") if isinstance(nxt, dict) else None
                    if (
                        isinstance(nxt_para, dict)
                        and not nxt_para.get("bullet")
                        and _is_code_line(nxt_para)
                    ):
                        lines.append(_code_line_text(nxt_para))
                        index += 1
                    else:
                        break
                _warn_once(warnings, seen, "monospace paragraphs converted to code block")
                blocks.append(_build_code_block(lines))
                continue

            blocks.extend(
                _build_paragraph_blocks(paragraph, scope, warnings, seen)
            )
            index += 1
            continue

        table = element.get("table")
        if isinstance(table, dict):
            built = _build_table(table, scope, warnings, seen)
            if built is not None:
                blocks.append(built)
            index += 1
            continue

        toc = element.get("tableOfContents")
        if isinstance(toc, dict):
            _warn_once(warnings, seen, "table of contents flattened")
            blocks.extend(
                _convert_structural_content(toc.get("content"), scope, warnings, seen)
            )
            index += 1
            continue

        if "sectionBreak" in element:
            # Section breaks have no body representation.
            index += 1
            continue

        index += 1

    return blocks


# --- Code blocks -------------------------------------------------------------


def _is_code_line(paragraph: dict) -> bool:
    """True if this paragraph is a pure code line (every textRun is monospace).

    A heading is never a code line. A paragraph qualifies only when it has at
    least one textRun and contains NO non-textRun element. Any non-textRun
    element (inline image, horizontal rule, person/richLink/footnote/equation
    smart chip) disqualifies it, so the paragraph takes the normal path — where
    monospace runs still get an inline ``code`` mark and the chip's text is
    preserved instead of being silently dropped by the code-block collector.
    """
    style = paragraph.get("paragraphStyle") or {}
    named = style.get("namedStyleType")
    if isinstance(named, str) and (named in _HEADING_LEVELS or named == _TITLE_STYLE):
        return False
    elements = paragraph.get("elements") or []
    saw_text_run = False
    for element in elements:
        if not isinstance(element, dict):
            continue
        if "textRun" not in element:
            # Any smart chip / image / rule disqualifies the code-line heuristic.
            return False
        run = element.get("textRun") or {}
        text_style = run.get("textStyle") or {}
        if not _is_monospace(_run_font_family(text_style)):
            return False
        saw_text_run = True
    return saw_text_run


def _code_line_text(paragraph: dict) -> str:
    """Collect a code line's text (terminating newline stripped, sentinel removed).

    Concatenates all textRun content, then strips a single trailing newline from
    the joined text (the paragraph terminator), so the result is independent of
    which element carries it.
    """
    elements = paragraph.get("elements") or []
    parts: list[str] = []
    for element in elements:
        if not isinstance(element, dict) or "textRun" not in element:
            continue
        parts.append((element.get("textRun") or {}).get("content") or "")
    text = "".join(parts)
    if text.endswith("\n"):
        text = text[:-1]
    return text.replace(_SOFT_BREAK, "\n").replace(_INLINE_OBJECT_SENTINEL, "")


def _build_code_block(lines: list[str]) -> dict:
    """Build a Docmost ``codeBlock`` joining ``lines`` with newlines."""
    code = "\n".join(lines)
    content = [{"type": "text", "text": code}] if code else []
    return {"type": "codeBlock", "attrs": {"language": None}, "content": content}


# --- Tables ------------------------------------------------------------------


def _coerce_span(value: Any) -> int:
    """Coerce a column/row span value to a positive int (fallback to 1)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _build_table(
    table: dict,
    scope: _Scope,
    warnings: list[str] | None,
    seen: set[str],
) -> dict | None:
    """Build a Docmost ``table`` from a Google Docs Table, or None when empty.

    Each ``tableCell`` recurses with the same block converter. Header rows
    (``tableRowStyle.tableHeader``) use ``tableHeader`` cells. ``columnSpan``/
    ``rowSpan`` map to ``colspan``/``rowspan``.
    """
    rows_out: list[dict] = []
    for row in table.get("tableRows") or []:
        if not isinstance(row, dict):
            continue
        is_header = bool((row.get("tableRowStyle") or {}).get("tableHeader"))
        cells_out: list[dict] = []
        for cell in row.get("tableCells") or []:
            if not isinstance(cell, dict):
                continue
            cell_style = cell.get("tableCellStyle") or {}

            cell_blocks = _convert_structural_content(
                cell.get("content"), scope, warnings, seen
            )
            if not cell_blocks:
                cell_blocks = [{"type": "paragraph"}]
            cells_out.append(
                {
                    "type": "tableHeader" if is_header else "tableCell",
                    "attrs": {
                        "colspan": _coerce_span(cell_style.get("columnSpan", 1)),
                        "rowspan": _coerce_span(cell_style.get("rowSpan", 1)),
                        "colwidth": None,
                    },
                    "content": cell_blocks,
                }
            )
        if cells_out:
            rows_out.append({"type": "tableRow", "content": cells_out})

    if not rows_out:
        _warn(warnings, "empty table dropped")
        return None
    return {"type": "table", "content": rows_out}


# --- Tab walking -------------------------------------------------------------


def _scope_from_maps(
    lists: Any, inline_objects: Any, positioned_objects: Any
) -> _Scope:
    """Build a ``_Scope`` from possibly-missing map dicts."""
    return _Scope(
        lists if isinstance(lists, dict) else {},
        inline_objects if isinstance(inline_objects, dict) else {},
        positioned_objects if isinstance(positioned_objects, dict) else {},
    )


def _walk_tabs(
    tabs: list[Any],
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Walk tabs depth-first (root order, then childTabs), per-tab scope.

    Each tab's content resolves its maps from ``documentTab.*`` (NOT the document
    top level), as the spec requires.
    """
    blocks: list[dict] = []
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        doc_tab = tab.get("documentTab")
        if isinstance(doc_tab, dict):
            body = doc_tab.get("body") or {}
            scope = _scope_from_maps(
                doc_tab.get("lists"),
                doc_tab.get("inlineObjects"),
                doc_tab.get("positionedObjects"),
            )
            blocks.extend(
                _convert_structural_content(body.get("content"), scope, warnings, seen)
            )
        child_tabs = tab.get("childTabs")
        if isinstance(child_tabs, list) and child_tabs:
            blocks.extend(_walk_tabs(child_tabs, warnings, seen))
    return blocks


# --- Public API --------------------------------------------------------------


def gdoc_to_docmost_doc(gdoc: Any, warnings: list[str] | None = None) -> dict:
    """Convert a Google Docs "Document" into a Docmost-shaped (TipTap) doc.

    Returns ``{"type":"doc","content":[...]}`` using only the node/mark types the
    Habr converter (``src.converter``) understands. ``gdoc`` may be the Document
    dict, a JSON string of it, or a thin wrapper. Unsupported features degrade
    gracefully and append a human-readable string to ``warnings`` (when given).

    Tabs vs legacy: when ``document.tabs`` is present and non-empty, content is
    taken from every tab's ``documentTab.body.content`` (root tabs in order, then
    childTabs depth-first) with that tab's own ``lists``/``inlineObjects``/
    ``positionedObjects`` as scope. Otherwise the legacy top-level ``body.content``
    is used with the top-level maps.
    """
    doc = _as_gdoc(gdoc)
    seen: set[str] = set()

    tabs = doc.get("tabs")
    if isinstance(tabs, list) and tabs:
        content = _walk_tabs(tabs, warnings, seen)
    else:
        scope = _scope_from_maps(
            doc.get("lists"),
            doc.get("inlineObjects"),
            doc.get("positionedObjects"),
        )
        body = doc.get("body") or {}
        content = _convert_structural_content(
            body.get("content"), scope, warnings, seen
        )

    return {"type": "doc", "content": content}
