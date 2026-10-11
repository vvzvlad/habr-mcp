"""Pure converter: Docmost (TipTap) ProseMirror doc -> Habr editorVersion-2 doc.

Docmost stores wiki pages as a TipTap/ProseMirror JSON tree. Habr's article
editor ("editorVersion 2") uses a *different* ProseMirror schema (see
``docs/habr-publication-protocol.md`` section 5). This module translates one tree
into the other.

Everything here is pure: no network, no file I/O. The HTTP/author layer (image
upload, save, etc.) lives elsewhere and calls these functions:

- ``collect_image_srcs``     -> which images need uploading to habrastorage,
- ``docmost_to_habr_doc``    -> the actual tree translation (src rewritten),
- ``serialize_source``       -> compact JSON string for ``postForm.text.source``,
- ``make_preview_doc``       -> the announce ("preview") doc from caller-supplied
  text (a separate field — never derived from the article body).

Handled Docmost node families (everything else degrades gracefully):
- block: ``paragraph``, ``heading``, ``codeBlock``, ``horizontalRule``,
  ``blockquote``, ``bulletList``/``orderedList``/``listItem`` (+ ``taskList``/
  ``taskItem``), ``callout``, ``details``, ``image``, ``table`` (with
  ``tableRow``/``tableCell``/``tableHeader``), ``mathBlock``, ``embed``/
  ``youtube``, ``drawio``/``excalidraw`` (converted to a Habr image via the
  rendered raster in ``attrs.src``);
- inline: ``text`` (+ marks), ``hardBreak``, ``mention``, ``mathInline``.

Design notes:
- We never emit a node/mark ``type`` that is not part of the Habr schema. Unknown
  inputs degrade gracefully and (optionally) record a human-readable warning.
- Warnings are appended to the caller-provided ``warnings`` list. Some warnings
  are de-duplicated ("once" warnings) so a long document does not spam them.
- Blocking content losses (an image/diagram without a habrastorage URL, an
  unknown block atom not in ``_HARMLESS_BLOCK_ATOMS``) are additionally appended
  to the optional ``losses`` list as ``(node_type, src_key)``. The converter
  never raises on a loss; the caller decides whether a loss aborts the save.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

# --- Habr editorVersion-2 constants -----------------------------------------

# Habr renders heading level 1 -> <h2>, 2 -> <h3>, 3 -> <h4> (h1 is the article
# title), so Docmost heading levels are clamped into this 1..3 range.
_MIN_HEADING_LEVEL = 1
_MAX_HEADING_LEVEL = 3

# Subtrees whose descendant headings must NOT count toward the document's heading
# baseline. The converter flattens any non-paragraph table-cell content to plain
# text, so a heading inside a cell never renders as a heading and must not skew
# the normalization baseline.
_NON_HEADING_SUBTREES = {"table", "tableRow", "tableCell", "tableHeader"}

# Habr requires the rendered announce (postForm.preview) text to be 100..3000
# characters (HTTP 422 otherwise). We hard-cap at the upper bound here; the
# lower bound is enforced by the caller (client.create_draft), which can raise a
# clear error instead of silently padding.
_PREVIEW_MAX_CHARS = 3000

# Docmost mark type -> Habr mark type. Marks not listed here are either dropped
# silently (highlight/textStyle/comment), handled explicitly (``spoiler`` keeps
# its text and emits a once-warning, since Habr has no inline spoiler mark), or
# dropped with a warning (anything truly unknown). ``link`` is handled specially
# (its attrs are rewritten).
_MARK_RENAME = {
    "bold": "bold",
    "italic": "italic",
    "strike": "strike",
    "underline": "underline",
    "code": "code",
    "subscript": "sub",
    "superscript": "sup",
}

# Marks we intentionally drop while keeping the underlying text. No warning is
# emitted for these because the loss is expected/cosmetic.
_MARK_DROP_SILENT = {"highlight", "textStyle", "comment"}

# Docmost callout semantic type -> Russian spoiler title. Looked up
# case-insensitively; an unknown or missing type falls back to "Спойлер" so the
# spoiler label stays in Russian regardless of the callout flavour.
_CALLOUT_TITLES = {
    "info": "Примечание",
    "warning": "Внимание",
    "danger": "Важно",
    "success": "Готово",
}
_CALLOUT_DEFAULT_TITLE = "Спойлер"


# --- Input normalization -----------------------------------------------------


def _as_doc(value: Any) -> dict:
    """Return the actual ``{"type":"doc",...}`` dict from a flexible input.

    Accepts (a) the doc itself, (b) a Docmost ``get_page_json`` page object that
    holds the doc under a ``content`` key (a dict doc or a bare content list), or
    (c) a JSON string of either of those. Raises ``ValueError`` otherwise.
    """
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("not a ProseMirror document")
    if value.get("type") == "doc":
        return value
    content = value.get("content")
    if isinstance(content, dict) and content.get("type") == "doc":
        return content
    if isinstance(content, list):
        return {"type": "doc", "content": content}
    raise ValueError("not a ProseMirror document")


def _children(node: Any) -> list:
    """Child list of ``node``, or ``[]`` when ``content`` is missing or not a list.

    ProseMirror children always live in a list; centralizing this guard keeps
    every tree walk total against malformed input where ``content`` is a
    non-list (e.g. a stray scalar), mirroring the existing ``isinstance(..., list)``
    guards in ``_convert_inline`` / ``_convert_blocks``.
    """
    content = node.get("content") if isinstance(node, dict) else None
    return content if isinstance(content, list) else []


# --- Heading-level normalization --------------------------------------------


def _min_heading_level(doc: dict) -> int:
    """Smallest heading level used anywhere in the doc that will actually render
    as a heading (default 1). Used to normalize so the document's top heading
    becomes Habr level 1 (<h2>); Docmost bodies usually start at H2 because the
    page title is a separate field. Table cells are skipped because the converter
    flattens any non-paragraph cell content to plain text, so a heading inside a
    cell never renders as a heading and must not skew the baseline."""
    levels: list[int] = []

    def visit(n: Any) -> None:
        if not isinstance(n, dict):
            return
        if n.get("type") in _NON_HEADING_SUBTREES:
            return
        if n.get("type") == "heading":
            try:
                levels.append(int((n.get("attrs") or {}).get("level", 1)))
            except (TypeError, ValueError):
                levels.append(1)
        for child in _children(n):
            visit(child)

    visit(doc)
    return min(levels) if levels else 1


# --- Warning bookkeeping -----------------------------------------------------


def _warn(warnings: list[str] | None, message: str) -> None:
    """Append ``message`` to ``warnings`` (no-op if ``warnings`` is None)."""
    if warnings is not None:
        warnings.append(message)


def _warn_once(warnings: list[str] | None, seen: set[str], message: str) -> None:
    """Append ``message`` at most once across the whole conversion.

    ``seen`` is a per-conversion set tracking already-emitted "once" messages so
    repeated nodes (e.g. many ``taskItem``s) do not spam identical warnings.
    """
    if warnings is None or message in seen:
        return
    seen.add(message)
    warnings.append(message)


def _record_loss(
    losses: list[tuple[str, str | None]] | None, ntype: str, src: Any
) -> None:
    """Append ``(ntype, image_src_key(src))`` to ``losses`` (no-op if None).

    A loss is content that would silently vanish from the Habr article (an
    image/diagram with no habrastorage URL, an unknown block atom); the caller
    uses the list to refuse the save.
    """
    if losses is not None:
        losses.append((ntype, image_src_key(src)))


# --- Public API: image collection -------------------------------------------


def image_src_key(src: Any) -> str | None:
    """Canonical map key for an image ``attrs.src``.

    ``src`` may be a plain URL/data-URI string or an MCP ``resource_link`` dict
    (``{"type": "resource_link", "uri": ...}``). Returns the string itself when
    it is a non-empty str, the ``uri`` when it is such a resource_link, else None.
    """
    if isinstance(src, str):
        return src if src else None
    if isinstance(src, dict) and src.get("type") == "resource_link":
        uri = src.get("uri")
        if isinstance(uri, str) and uri:
            return uri
    return None


def collect_image_srcs(docmost_doc: dict) -> list[Any]:
    """De-duplicated original ``attrs.src`` VALUES for every Docmost ``image``
    and diagram node (``drawio``/``excalidraw``).

    A drawio/excalidraw node carries a rendered raster (PNG/SVG) in ``attrs.src``
    exactly like an image, so its src is collected for the same habrastorage
    upload. Each value is returned as-is (a str URL/data-URI OR a
    ``resource_link`` dict) in document order, de-duplicated by
    ``image_src_key``. Values whose key is None (empty/missing/unsupported src)
    are skipped. Only ``_reupload_images`` consumes this.
    """
    doc = _as_doc(docmost_doc)
    srcs: list[Any] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") in ("image", "drawio", "excalidraw"):
            src = (node.get("attrs") or {}).get("src")
            key = image_src_key(src)
            if key is not None and key not in seen:
                seen.add(key)
                srcs.append(src)
        for child in _children(node):
            visit(child)

    visit(doc)
    return srcs


# --- Marks -------------------------------------------------------------------


def _convert_marks(
    docmost_marks: Any,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a Docmost ``marks`` array into Habr marks.

    Unknown marks are dropped with a warning; ``highlight``/``textStyle``/
    ``comment`` are dropped silently; ``spoiler`` keeps its text and emits a
    once-warning (Habr has no inline spoiler mark); ``link`` keeps only its
    ``href``.
    """
    result: list[dict] = []
    if not isinstance(docmost_marks, list):
        return result
    for mark in docmost_marks:
        if not isinstance(mark, dict):
            continue
        mtype = mark.get("type")
        if mtype in _MARK_RENAME:
            result.append({"type": _MARK_RENAME[mtype]})
        elif mtype == "link":
            href = (mark.get("attrs") or {}).get("href")
            result.append({"type": "link", "attrs": {"href": href}})
        elif mtype == "abbr":
            # The footnote preprocessing writes Docmost-flavoured abbr marks onto
            # text nodes; translate them to Habr's abbr mark (hover tooltip),
            # keeping the title. Without this branch they'd be dropped as unknown.
            # An abbr with no usable title carries no tooltip, so drop it (keep the
            # text unmarked) rather than emit an empty/null title.
            title = (mark.get("attrs") or {}).get("title")
            if isinstance(title, str) and title:
                result.append({"type": "abbr", "attrs": {"title": title}})
        elif mtype in _MARK_DROP_SILENT:
            continue  # keep the text, drop the mark
        elif mtype == "spoiler":
            # Habr editorVersion-2 has no inline spoiler mark (only the block-level
            # ``spoiler`` node), so an inline spoiler cannot be represented. Keep the
            # text and warn once that the previously hidden text is now visible.
            _warn_once(warnings, seen, "inline spoiler kept as plain text (Habr has no inline spoiler)")
        else:
            _warn(warnings, f"unsupported mark dropped: {mtype}")
    return result


# --- Mentions ----------------------------------------------------------------

# A bare ``@nick`` inside plain text. The lookbehind rejects ``@`` preceded by a
# word char / ``@`` / ``.`` / ``/`` / ``-`` so emails (user@example.com), paths,
# and doubled ``@@`` are skipped. A nick is 2..30 of ``[A-Za-z0-9_]``.
_MENTION_RE = re.compile(r"(?<![\w@./-])@([A-Za-z0-9_]{2,30})\b")

# A standalone nick (no leading ``@``) used to validate a Docmost mention label.
_NICK_RE = re.compile(r"^[A-Za-z0-9_]{2,30}$")


def _build_mention(nick: str) -> dict:
    """Build a Habr inline ``mention`` node for ``nick`` (leading ``@`` optional).

    Habr accepts a mention for any nick (it does not verify the user exists on
    save), so we only build the canonical node shape here.
    """
    nick = nick.lstrip("@").strip()
    return {
        "type": "mention",
        "attrs": {
            "identity": nick,
            "identityType": "user",
            "display": "@" + nick,
            "link": "/users/" + nick,
            "class": "mention",
        },
    }


def _split_text_with_mentions(text: str, marks: list[dict]) -> list[dict]:
    """Split ``text`` into interleaved text + ``mention`` inline nodes.

    Literal text segments carry ``marks`` (the already-converted marks of the
    source text node); ``mention`` nodes carry NO marks. When ``text`` has no
    ``@nick`` match, a single text node (with marks) is returned unchanged.
    Empty segments never produce empty text nodes.
    """

    def _text_node(segment: str) -> dict:
        node: dict[str, Any] = {"type": "text", "text": segment}
        if marks:
            node["marks"] = marks
        return node

    nodes: list[dict] = []
    pos = 0
    matched = False
    for match in _MENTION_RE.finditer(text):
        matched = True
        pre = text[pos:match.start()]
        if pre:
            nodes.append(_text_node(pre))
        nodes.append(_build_mention(match.group(1)))
        pos = match.end()
    if not matched:
        # No @nick: keep the original single text node (don't restructure).
        return [_text_node(text)]
    tail = text[pos:]
    if tail:
        nodes.append(_text_node(tail))
    return nodes


# --- Inline content ----------------------------------------------------------


def _convert_inline(
    children: Any,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a list of Docmost inline nodes into Habr inline nodes.

    Handles ``text`` (with marks), ``hardBreak`` -> ``hard_break``, and a few
    inline atoms (``mention``, ``mathInline``) that degrade to plain text.
    """
    result: list[dict] = []
    if not isinstance(children, list):
        return result
    for child in children:
        if not isinstance(child, dict):
            continue
        ctype = child.get("type")
        if ctype == "text":
            text = child.get("text", "")
            marks = _convert_marks(child.get("marks"), warnings, seen)
            # A ``code`` mark means a literal code span: never run @nick detection
            # inside it (e.g. "@media" must stay verbatim). A ``link`` mark means
            # the text is part of a hyperlink: a ``@nick`` inside it must keep its
            # href and stay linked text, not become a mention (which carries no
            # marks). In both cases emit the text verbatim as one node.
            skip_split = any(m.get("type") in ("code", "link") for m in marks)
            if skip_split:
                node: dict[str, Any] = {"type": "text", "text": text}
                if marks:
                    node["marks"] = marks
                result.append(node)
            else:
                # Split out any bare ``@nick`` occurrences into mention nodes,
                # keeping the surrounding literal text (with its marks) intact.
                result.extend(_split_text_with_mentions(text, marks))
        elif ctype == "hardBreak":
            result.append({"type": "hard_break"})
        elif ctype == "mention":
            # Docmost mention node. Emit a real Habr mention when the label maps
            # to a single-token nick and the entityType is user (or unknown but
            # nick-shaped); otherwise fall back to plain text using the label.
            attrs = child.get("attrs") or {}
            label = attrs.get("label") or "@?"
            entity_type = attrs.get("entityType")
            nick = label.lstrip("@").strip()
            nick_ok = bool(_NICK_RE.match(nick))
            user_like = entity_type == "user" or (
                entity_type in (None, "", "unknown") and nick_ok
            )
            if user_like and nick_ok:
                result.append(_build_mention(nick))
            else:
                # e.g. entityType=="page" or a multi-word display label: degrade
                # to plain text using the label.
                result.append({"type": "text", "text": label})
                _warn_once(warnings, seen, "mention converted to plain text")
        elif ctype == "mathInline":
            # Habr has a native inline LaTeX node; source lives in attrs.source.
            # Use .strip() ONLY to decide emptiness so a whitespace-only source
            # (e.g. "   ") is treated as empty and dropped; emit the ORIGINAL
            # (un-stripped) source when non-empty so interior LaTeX spaces survive.
            text = (child.get("attrs") or {}).get("text") or ""
            latex = text.strip()
            if latex:
                result.append({"type": "inline_formula", "attrs": {"source": text}})
            else:
                _warn_once(warnings, seen, "empty mathInline dropped")
        else:
            _warn(warnings, f"unsupported inline dropped: {ctype}")
    return result


# --- Code-block text extraction ---------------------------------------------


def _collect_code_text(node: Any) -> str:
    """Concatenate all descendant text of ``node``; ``hardBreak`` -> newline.

    Used to rebuild a code-block's source, which Habr stores as a single string
    in ``attrs.code`` rather than as child nodes.
    """
    if not isinstance(node, dict):
        return ""
    ntype = node.get("type")
    if ntype == "text":
        return node.get("text", "") or ""
    if ntype == "hardBreak":
        return "\n"
    parts: list[str] = []
    for child in _children(node):
        parts.append(_collect_code_text(child))
    return "".join(parts)


# --- Block builders ----------------------------------------------------------


def _build_paragraph(
    node: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Build a Habr ``paragraph``. Empty paragraphs omit the ``content`` key."""
    align = (node.get("attrs") or {}).get("textAlign")
    inline = _convert_inline(node.get("content"), warnings, seen)
    if inline:
        # Habr's editor omits the ``align`` key entirely when alignment is null;
        # only include it when a real value is present (canonical shape).
        attrs: dict[str, Any] = {"simple": False, "persona": False}
        if align:
            attrs["align"] = align
        return {"type": "paragraph", "attrs": attrs, "content": inline}
    # An empty (e.g. trailing) paragraph carries no content key and no align.
    return {"type": "paragraph", "attrs": {"simple": False, "persona": False}}


def _build_heading(
    node: dict,
    heading_min_level: int,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Build a Habr ``heading`` normalized against ``heading_min_level`` then
    clamped to 1..3 (default 1).

    Normalization shifts every heading so the document's top heading level maps
    to Habr level 1 (<h2>); Docmost bodies usually start at H2 because the page
    title is a separate field, so without this the whole document would render
    one size too small.
    """
    raw_level = (node.get("attrs") or {}).get("level", 1)
    try:
        raw = int(raw_level)
    except (TypeError, ValueError):
        raw = 1
    level = raw - heading_min_level + 1  # normalize: doc's top heading -> 1
    level = max(_MIN_HEADING_LEVEL, min(_MAX_HEADING_LEVEL, level))  # clamp 1..3
    return {
        "type": "heading",
        "attrs": {"level": level, "class": None},
        "content": _convert_inline(node.get("content"), warnings, seen),
    }


def _build_code_block(node: dict) -> dict:
    """Build a Habr ``code_block`` (code in ``attrs.code``, no content key)."""
    attrs = node.get("attrs") or {}
    lang = attrs.get("language") or attrs.get("lang")
    code = _collect_code_text(node)
    return {"type": "code_block", "attrs": {"lang": lang, "code": code}}


def _build_image(
    node: dict,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    kind: str = "image",
) -> dict | None:
    """Build a Habr ``image`` node, rewriting ``src`` via ``image_url_map``.

    The Docmost image caption (``attrs.caption``, mirrors ``data-caption``) is
    carried into Habr's ``image_caption`` child; a missing/blank caption stays
    empty. Returns ``None`` (and warns) if the original src has no mapped
    habrastorage URL, signalling the caller to drop the node. ``kind`` only
    tunes the drop-warning label (e.g. "drawio"/"excalidraw" for diagram nodes,
    which share this builder because their raster lives in ``attrs.src`` too).
    """
    attrs = node.get("attrs") or {}
    src = attrs.get("src")
    key = image_src_key(src)
    if not image_url_map or key is None or key not in image_url_map:
        _warn(warnings, f"{kind} dropped (no habrastorage url): {src}")
        return None
    new_src = image_url_map[key]

    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # Carry the Docmost image caption (``attrs.caption``, mirrors data-caption)
    # into Habr's ``image_caption`` child. A missing or blank caption stays empty.
    caption = attrs.get("caption")
    if isinstance(caption, str) and caption.strip():
        caption_node = {"type": "image_caption", "content": [{"type": "text", "text": caption}]}
    else:
        caption_node = {"type": "image_caption"}

    return {
        "type": "image",
        "attrs": {
            "src": new_src,
            "alt": attrs.get("alt"),
            "title": attrs.get("title"),
            "width": _coerce_int(attrs.get("width")),
            "height": _coerce_int(attrs.get("height")),
            "fullWidth": True,
            "border": False,
            "float": False,
            "customClass": "",
            "gallery": False,
            "inserted": False,
        },
        "content": [caption_node],
    }


def _build_spoiler(title: str, children: list[dict]) -> dict:
    """Build a Habr ``spoiler`` (details/callout collapse) with a title."""
    return {"type": "spoiler", "attrs": {"title": title}, "content": children}


# --- Table -------------------------------------------------------------------


def _build_table_paragraph(
    node: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Build a Habr ``table_paragraph`` from a Docmost ``paragraph``.

    Cells use ``table_paragraph`` (not ``paragraph``); ``align`` is null unless the
    source paragraph carries a real ``textAlign``. The ``content`` key is omitted
    when the paragraph has no inline content.
    """
    align = (node.get("attrs") or {}).get("textAlign") or None
    inline = _convert_inline(node.get("content"), warnings, seen)
    para: dict[str, Any] = {"type": "table_paragraph", "attrs": {"align": align}}
    if inline:
        para["content"] = inline
    return para


def _build_table_cell(
    node: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Build a Habr ``table_cell`` from a Docmost ``tableCell``/``tableHeader``.

    Habr has no distinct header cell, so a ``tableHeader`` also maps here (header
    styling is lost). ``colspan``/``rowspan`` default to 1 and are coerced to int;
    ``colwidth`` is passed through as-is (default null). Each block child becomes a
    ``table_paragraph``: a real paragraph maps directly, any other block (nested
    list, blockquote, …) is flattened to a ``table_paragraph`` carrying that
    block's collected inline text (and a once-warning). A cell always contains at
    least one ``table_paragraph``.
    """
    attrs = node.get("attrs") or {}

    def _coerce_span(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    cell_attrs = {
        "colspan": _coerce_span(attrs.get("colspan", 1)),
        "rowspan": _coerce_span(attrs.get("rowspan", 1)),
        "colwidth": attrs.get("colwidth"),
    }

    paragraphs: list[dict] = []
    for child in _children(node):
        if not isinstance(child, dict):
            continue
        if child.get("type") == "paragraph":
            paragraphs.append(_build_table_paragraph(child, warnings, seen))
        else:
            # Complex cell content (nested list, quote, etc.) is not representable
            # as-is inside a Habr cell: flatten it to a single table_paragraph of
            # its collected inline text (best effort) and warn once.
            text = _collect_plain_text(child)
            flat: dict[str, Any] = {"type": "table_paragraph", "attrs": {"align": None}}
            if text:
                flat["content"] = [{"type": "text", "text": text}]
            paragraphs.append(flat)
            _warn_once(
                warnings, seen, "complex table cell content flattened to text"
            )

    if not paragraphs:
        # Cells must contain at least one table_paragraph (empty otherwise).
        paragraphs.append({"type": "table_paragraph", "attrs": {"align": None}})

    return {"type": "table_cell", "attrs": cell_attrs, "content": paragraphs}


def _build_table(
    node: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Build a Habr ``table_wrapper`` > ``table`` from a Docmost ``table``.

    Docmost ``tableRow`` -> ``table_row``; ``tableCell``/``tableHeader`` ->
    ``table_cell``. A table with zero rows is dropped with a warning (returns []).
    """
    rows: list[dict] = []
    for row in _children(node):
        if not isinstance(row, dict) or row.get("type") != "tableRow":
            continue
        cells: list[dict] = []
        for cell in _children(row):
            if not isinstance(cell, dict):
                continue
            if cell.get("type") in ("tableCell", "tableHeader"):
                cells.append(_build_table_cell(cell, warnings, seen))
        if not cells:
            # A row whose children yield no cells would become an empty table_row
            # (content: []), which Habr rejects. Skip it instead of emitting it.
            _warn(warnings, "empty table row skipped")
            continue
        rows.append({"type": "table_row", "content": cells})

    if not rows:
        _warn(warnings, "empty table dropped")
        return []

    return [
        {
            "type": "table_wrapper",
            "content": [{"type": "table", "content": rows}],
        }
    ]


# --- Block dispatch ----------------------------------------------------------

# Simple wrapper nodes whose only job is to map a Docmost block name to a Habr
# block name and recurse into BLOCK children. (Lists, blockquote, list items.)
# Habr's list item node is named ``listitem`` (one word, lowercase) — emitting
# ``list_item`` (snake_case) makes the editor-2 form reject text.source with 422.
_WRAPPER_RENAME = {
    "blockquote": "blockquote",
    "bulletList": "unordered_list",
    "orderedList": "ordered_list",
    "listItem": "listitem",
    # taskList/taskItem reuse the plain list nodes; checkbox state is lost.
    "taskList": "unordered_list",
    "taskItem": "listitem",
}

# Unknown block atoms without visible content: dropped with a warning like any
# unknown atom, but NOT recorded as a content loss (they never block a save).
_HARMLESS_BLOCK_ATOMS = {"pageBreak"}

# Habr block types confirmed safe inside a listitem. Anything else (code_block,
# image, blockquote, spoiler, …) is kept but flagged, since the list zone may
# reject non-paragraph/non-list children. Nested lists carry attrs.type "inner".
_LIST_ITEM_SAFE_TYPES = {"paragraph", "unordered_list", "ordered_list"}

# Habr list nodes that must carry attrs.type ("outer" top-level, "inner" nested).
_LIST_TYPES = {"unordered_list", "ordered_list"}


def _convert_blocks(
    children: Any,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
    nested_list: bool = False,
    heading_min_level: int = 1,
    *,
    losses: list[tuple[str, str | None]] | None = None,
) -> list[dict]:
    """Convert a list of Docmost block nodes into a flat list of Habr blocks.

    Unknown wrappers are flattened (their block children spliced into the parent
    stream) and unknown atoms are dropped; both record a warning. ``nested_list``
    is True while converting a listitem's children, so a list directly inside a
    list item is tagged ``attrs.type:"inner"`` instead of ``"outer"``.
    ``heading_min_level`` is the doc-wide heading baseline threaded down so every
    heading (at any nesting depth) normalizes against the same value.
    ``losses`` collects blocking content losses (see ``_record_loss``).
    """
    result: list[dict] = []
    if not isinstance(children, list):
        return result
    for child in children:
        if not isinstance(child, dict):
            continue
        result.extend(
            _convert_block(
                child,
                image_url_map,
                warnings,
                seen,
                nested_list,
                heading_min_level,
                losses=losses,
            )
        )
    return result


def _convert_block(
    node: dict,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
    nested_list: bool = False,
    heading_min_level: int = 1,
    *,
    losses: list[tuple[str, str | None]] | None = None,
) -> list[dict]:
    """Convert a single Docmost block node into zero or more Habr blocks.

    Returns a list so a node can expand to several blocks (e.g. a flattened
    wrapper) or to none (e.g. a dropped atom or an unmapped image).
    ``nested_list`` controls the list ``attrs.type`` (inner vs outer).
    ``heading_min_level`` is the doc-wide heading baseline used to normalize
    heading levels regardless of nesting depth. An unmapped image/diagram and
    an unknown block atom (outside ``_HARMLESS_BLOCK_ATOMS``) are recorded in
    ``losses``.
    """
    ntype = node.get("type")

    if ntype == "paragraph":
        return [_build_paragraph(node, warnings, seen)]
    if ntype == "heading":
        return [_build_heading(node, heading_min_level, warnings, seen)]
    if ntype == "codeBlock":
        return [_build_code_block(node)]
    if ntype == "horizontalRule":
        return [{"type": "hr", "attrs": {"inserted": True}}]

    if ntype in _WRAPPER_RENAME:
        habr_type = _WRAPPER_RENAME[ntype]
        if ntype == "taskList" or ntype == "taskItem":
            _warn_once(
                warnings, seen, "task list converted to plain list (checkbox state lost)"
            )
        is_list_item = ntype in ("listItem", "taskItem")
        # A listitem's children recurse with nested_list=True so a list inside
        # it is tagged "inner"; a list's own items keep the current nesting flag.
        inner = _convert_blocks(
            node.get("content"),
            image_url_map,
            warnings,
            seen,
            nested_list=True if is_list_item else nested_list,
            heading_min_level=heading_min_level,
            losses=losses,
        )
        if is_list_item:
            # Block content other than paragraphs and nested lists may be
            # rejected by the list zone. Keep it (non-lossy) but warn once so the
            # user knows the result may not import. Items carry NO attrs.
            if any(
                block.get("type") not in _LIST_ITEM_SAFE_TYPES for block in inner
            ):
                _warn_once(
                    warnings,
                    seen,
                    "list item contains block content Habr may reject "
                    "(code/image/quote/etc.)",
                )
            return [{"type": habr_type, "content": inner}]
        if habr_type in _LIST_TYPES:
            # Lists carry attrs.type: "inner" when directly inside a list item,
            # "outer" at top level (or inside blockquote/spoiler/etc.).
            list_kind = "inner" if nested_list else "outer"
            return [{"type": habr_type, "attrs": {"type": list_kind}, "content": inner}]
        return [{"type": habr_type, "content": inner}]

    if ntype == "callout":
        # Russian title from the callout's semantic type (info/warning/danger/
        # success), looked up case-insensitively; unknown/missing -> "Спойлер".
        callout_type = (node.get("attrs") or {}).get("type")
        key = callout_type.lower() if isinstance(callout_type, str) else ""
        title = _CALLOUT_TITLES.get(key, _CALLOUT_DEFAULT_TITLE)
        inner = _convert_blocks(
            node.get("content"),
            image_url_map,
            warnings,
            seen,
            heading_min_level=heading_min_level,
            losses=losses,
        )
        return [_build_spoiler(title, inner)]

    if ntype == "details":
        return [
            _convert_details(
                node, image_url_map, warnings, seen, heading_min_level, losses=losses
            )
        ]

    if ntype == "image":
        built = _build_image(node, image_url_map, warnings)
        if built is None:
            _record_loss(losses, "image", (node.get("attrs") or {}).get("src"))
            return []
        return [built]

    if ntype in ("drawio", "excalidraw"):
        # A diagram node carries its rendered raster in attrs.src, exactly like
        # an image, so it converts to a Habr image via the same builder.
        built = _build_image(node, image_url_map, warnings, kind=ntype)
        if built is None:
            _record_loss(losses, ntype, (node.get("attrs") or {}).get("src"))
            return []
        return [built]

    if ntype == "table":
        return _build_table(node, warnings, seen)

    if ntype == "mathBlock":
        # Block LaTeX: Habr's "formula" node carries the source in attrs.source.
        # Use .strip() ONLY to decide emptiness so a whitespace-only source
        # (e.g. "   ") is dropped; emit the ORIGINAL (un-stripped) source when
        # non-empty so interior LaTeX whitespace is preserved.
        text = (node.get("attrs") or {}).get("text") or ""
        latex = text.strip()
        if not latex:
            _warn(warnings, "empty mathBlock dropped")
            return []
        return [{"type": "formula", "attrs": {"source": text}}]

    if ntype in ("embed", "youtube"):
        # Both store the URL in attrs.src. Habr resolves the oEmbed server-side
        # (GET https://embedd.srv.habr.com/geturl), so we never fetch anything.
        src = (node.get("attrs") or {}).get("src")
        if not isinstance(src, str) or not src:
            _warn(warnings, f"embed dropped (no src): {ntype}")
            return []
        return [{"type": "embed", "attrs": {"src": src, "inserted": False}}]

    if ntype in ("tableRow", "tableCell", "tableHeader"):
        # These are only ever built inside _build_table; a stray one at dispatch
        # level has no valid Habr standalone form, so drop it with a warning.
        _warn(warnings, f"unsupported block dropped: {ntype}")
        return []

    # Fallback for ANY other block (columns, video, ...): if it has block
    # children, flatten them into the parent stream so we never silently lose
    # nested content; if it is an atom, drop it. Both record a warning. A
    # dropped atom is also a content loss unless it is known to be harmless.
    children = node.get("content")
    if isinstance(children, list) and children:
        _warn(warnings, f"unsupported block flattened: {ntype}")
        return _convert_blocks(
            children,
            image_url_map,
            warnings,
            seen,
            nested_list,
            heading_min_level,
            losses=losses,
        )
    _warn(warnings, f"unsupported block dropped: {ntype}")
    if ntype not in _HARMLESS_BLOCK_ATOMS:
        # Unknown nodes may carry any attrs shape; the converter must stay total.
        attrs = node.get("attrs")
        _record_loss(
            losses, ntype, attrs.get("src") if isinstance(attrs, dict) else None
        )
    return []


def _convert_details(
    node: dict,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
    heading_min_level: int = 1,
    *,
    losses: list[tuple[str, str | None]] | None = None,
) -> dict:
    """Convert a Docmost ``details`` node into a Habr ``spoiler``.

    Title comes from the ``detailsSummary`` child's text; content comes from the
    converted block children of the ``detailsContent`` child. ``heading_min_level``
    is threaded so headings inside the spoiler normalize against the doc baseline;
    ``losses`` collects blocking content losses from the spoiler body.
    """
    summary_text = ""
    content_children: list[dict] = []
    for child in _children(node):
        if not isinstance(child, dict):
            continue
        ctype = child.get("type")
        if ctype == "detailsSummary":
            summary_text = _collect_plain_text(child)
        elif ctype == "detailsContent":
            content_children = _convert_blocks(
                child.get("content"),
                image_url_map,
                warnings,
                seen,
                heading_min_level=heading_min_level,
                losses=losses,
            )
    title = summary_text.strip() or "Спойлер"
    return _build_spoiler(title, content_children)


def _collect_plain_text(node: Any) -> str:
    """Concatenate descendant ``text`` of ``node`` (for titles/previews)."""
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "") or ""
    parts: list[str] = []
    for child in _children(node):
        parts.append(_collect_plain_text(child))
    return "".join(parts)


# --- Footnotes -> abbr tooltips ----------------------------------------------

# Top-level heading titles (lowercased/stripped) that introduce a translator's
# footnotes section. The section is a heading followed by an orderedList whose
# i-th item is note i.
_FOOTNOTE_HEADINGS = {
    "примечания переводчика",
    "примечания",
    "сноски",
    "notes",
    "footnotes",
    "translator's notes",
}

# A plain-text footnote marker like ``[1]`` placed right after a word in the body.
_FOOTNOTE_MARKER_RE = re.compile(r"\[(\d+)\]")


def _apply_footnote_markers(
    inline_nodes: list,
    notes: dict[int, str],
    warnings: list[str] | None,
    seen: set[str],
) -> list:
    """Rewrite ``[N]`` markers in one inline list into abbr tooltips.

    For each marker whose number is in ``notes``: attach an ``abbr`` mark (title
    = the note text) to the last word BEFORE the marker, and delete the marker
    (plus a single preceding space, if any). Markers with no matching note or no
    preceding word are left as literal text (with a once-warning).
    """
    # Split the inline list into maximal runs of consecutive ``text`` nodes,
    # separated by non-text inline nodes (mention/hardBreak/mathInline/...), which
    # are passed through untouched to preserve order.
    result: list = []
    run: list[dict] = []

    def flush_run() -> None:
        if run:
            result.extend(_rewrite_text_run(run, notes, warnings, seen))
            run.clear()

    for node in inline_nodes:
        if isinstance(node, dict) and node.get("type") == "text":
            run.append(node)
        else:
            flush_run()
            result.append(node)
    flush_run()
    return result


def _rewrite_text_run(
    run: list[dict],
    notes: dict[int, str],
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Rewrite a single run of consecutive ``text`` nodes (see caller)."""
    # Build the combined string and a per-character parallel array of marks.
    combined_chars: list[str] = []
    char_marks: list[list] = []
    for node in run:
        text = node.get("text", "") or ""
        marks = node.get("marks") if isinstance(node.get("marks"), list) else []
        for ch in text:
            combined_chars.append(ch)
            char_marks.append(marks)
    combined = "".join(combined_chars)

    matches = [(m.start(), m.end(), int(m.group(1))) for m in _FOOTNOTE_MARKER_RE.finditer(combined)]
    # Every char index covered by ANY marker span. Used so the word-search can
    # SKIP OVER adjacent markers (e.g. ``word[1][2]``) to reach the real word,
    # letting several markers attach to the same word.
    blocked: set[int] = set()
    for m0, m1, _num in matches:
        blocked.update(range(m0, m1))

    deleted = [False] * len(combined)
    # Word span (start, end) -> ordered list of note titles attached to it. A
    # given word span is either shared by markers (identical span) or disjoint
    # from other words, so per-char titles never conflict.
    word_titles: dict[tuple[int, int], list[str]] = {}

    for m0, m1, num in matches:
        if num not in notes:
            _warn_once(warnings, seen, "footnote marker with no matching note left as text")
            continue
        # Optionally drop the single space directly before the marker.
        if m0 > 0 and combined[m0 - 1].isspace():
            del_start = m0 - 1
        else:
            del_start = m0
        # Skip trailing whitespace and adjacent marker spans to reach the real
        # word end (whitespace via isspace(); prior/next markers via ``blocked``).
        j = del_start
        while j > 0 and (combined[j - 1].isspace() or (j - 1) in blocked):
            j -= 1
        w_end = j
        w_start = j
        while w_start > 0 and not combined[w_start - 1].isspace() and (w_start - 1) not in blocked:
            w_start -= 1
        if w_start == w_end:
            _warn_once(warnings, seen, "footnote marker with no preceding word left as text")
            continue
        word_titles.setdefault((w_start, w_end), []).append(notes[num])
        for i in range(del_start, m1):
            deleted[i] = True

    # Build per-char abbr titles: each word span gets ONE merged title (its notes
    # joined with "; " in marker order, dropping consecutive duplicates).
    abbr_titles: list[str | None] = [None] * len(combined)
    for (w_start, w_end), titles in word_titles.items():
        merged: list[str] = []
        for t in titles:
            if not merged or merged[-1] != t:
                merged.append(t)
        joined = "; ".join(merged)
        for i in range(w_start, w_end):
            abbr_titles[i] = joined

    # Rebuild the run: keep non-deleted chars, merging consecutive chars whose
    # effective marks (original marks + abbr mark when set) are equal by value.
    rebuilt: list[dict] = []
    cur_text: list[str] = []
    cur_marks: list | None = None

    def flush() -> None:
        nonlocal cur_marks
        if cur_text:
            node: dict[str, Any] = {"type": "text", "text": "".join(cur_text)}
            if cur_marks:
                node["marks"] = cur_marks
            rebuilt.append(node)
            cur_text.clear()
            cur_marks = None

    for i, ch in enumerate(combined):
        if deleted[i]:
            continue
        marks = list(char_marks[i])
        if abbr_titles[i] is not None:
            marks = marks + [{"type": "abbr", "attrs": {"title": abbr_titles[i]}}]
        eff = marks if marks else []
        if cur_text and eff == (cur_marks or []):
            cur_text.append(ch)
        else:
            flush()
            cur_text.append(ch)
            cur_marks = eff
    flush()
    return rebuilt


def _find_footnote_section(content: list) -> tuple[int, int] | None:
    """Locate the notes section in a top-level content list.

    Returns ``(heading_index, list_index)`` for the FIRST footnote heading
    followed (skipping blank paragraphs) by an ``orderedList``, else ``None``.
    """
    for i, node in enumerate(content):
        if not isinstance(node, dict) or node.get("type") != "heading":
            continue
        title = _collect_plain_text(node).strip().lower()
        if title not in _FOOTNOTE_HEADINGS:
            continue
        # The notes list is the next top-level node, skipping blank paragraphs.
        j = i + 1
        while j < len(content):
            nxt = content[j]
            if not isinstance(nxt, dict):
                break
            ntype = nxt.get("type")
            if ntype == "orderedList":
                return (i, j)
            if ntype == "paragraph" and not _collect_plain_text(nxt).strip():
                j += 1
                continue
            break  # something else in between -> this heading does not qualify
        # This matching heading is not followed by an orderedList; keep scanning
        # for a later footnote heading that is (a lone "Notes" heading earlier in
        # the doc must not shadow the real notes section further down).
    return None


def _build_footnote_map(ordered_list: dict) -> dict[int, str]:
    """Build a number -> note-text map from the notes ``orderedList``."""
    attrs = ordered_list.get("attrs") or {}
    start_raw = attrs.get("start")
    start = start_raw if isinstance(start_raw, int) and start_raw > 0 else 1
    notes: dict[int, str] = {}
    p = 0
    for item in _children(ordered_list):
        if not isinstance(item, dict) or item.get("type") != "listItem":
            continue
        number = start + p
        p += 1
        text = " ".join(_collect_plain_text(item).split())
        if text:
            notes[number] = text
    return notes


def _inline_footnotes_as_abbr(
    doc: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Turn Docmost footnotes into Habr abbr tooltips (see module docstring).

    Attaches each note's text as an abbr tooltip on the last word before its
    ``[N]`` marker (removing the marker), and removes the bottom notes section
    (heading + orderedList). Returns a NEW doc; the input is never mutated. If no
    footnote section is found the doc is returned unchanged (no-op safety case).
    """
    doc = copy.deepcopy(doc)
    content = doc.get("content")
    if not isinstance(content, list):
        return doc

    located = _find_footnote_section(content)
    if located is None:
        return doc  # no matching heading + list -> leave markers alone
    heading_idx, list_idx = located

    notes = _build_footnote_map(content[list_idx])
    if not notes:
        return doc  # nothing usable -> leave the doc unchanged

    # Remove the notes section (heading + orderedList) by index/identity.
    new_content = [
        node for k, node in enumerate(content) if k not in (heading_idx, list_idx)
    ]

    referenced: set[int] = set()

    def rewrite(node: Any) -> None:
        """Recursively rewrite markers in every inline container in the tree."""
        if not isinstance(node, dict):
            return
        children = node.get("content")
        if isinstance(children, list):
            if any(isinstance(c, dict) and c.get("type") == "text" for c in children):
                before = _FOOTNOTE_MARKER_RE.findall(
                    "".join(
                        c.get("text", "")
                        for c in children
                        if isinstance(c, dict) and c.get("type") == "text"
                    )
                )
                node["content"] = _apply_footnote_markers(children, notes, warnings, seen)
                for num in before:
                    if int(num) in notes:
                        referenced.add(int(num))
            for c in children:
                rewrite(c)

    for node in new_content:
        rewrite(node)

    if set(notes) - referenced:
        _warn_once(warnings, seen, "some footnotes were never referenced by a marker")

    doc["content"] = new_content
    return doc


# --- Public API: full document conversion ------------------------------------


def docmost_to_habr_doc(
    docmost_doc: dict,
    image_url_map: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    losses: list[tuple[str, str | None]] | None = None,
) -> dict:
    """Translate a Docmost (TipTap) ProseMirror doc into a Habr editorVersion-2 doc.

    Returns ``{"type":"doc","content":[...]}``. Never emits a non-Habr node/mark
    type and never raises on lost or unknown content (only ``ValueError`` for an
    input that is not a ProseMirror doc). Unknown nodes/marks degrade gracefully
    and append a human-readable string to ``warnings`` (when a list is provided).
    ``image`` nodes get their ``src`` replaced via ``image_url_map``; an image
    whose src is not in the map is dropped with a warning. Blocking content
    losses (an unmapped image/drawio/excalidraw, an unknown block atom that is
    not in ``_HARMLESS_BLOCK_ATOMS``) are also appended to ``losses`` (when a
    list is provided) as ``(node_type, src_key)`` so the caller can refuse the
    save.
    """
    doc = _as_doc(docmost_doc)
    seen: set[str] = set()
    # Turn Docmost footnotes ([N] markers + bottom notes list) into Habr abbr tooltips.
    doc = _inline_footnotes_as_abbr(doc, warnings, seen)
    # Normalize so the document's top heading becomes Habr level 1 (<h2>); Docmost
    # bodies usually start at H2 because the page title is a separate field.
    heading_min_level = _min_heading_level(doc)
    content = _convert_blocks(
        doc.get("content"),
        image_url_map,
        warnings,
        seen,
        heading_min_level=heading_min_level,
        losses=losses,
    )
    return {"type": "doc", "content": content}


# --- Public API: serialization -----------------------------------------------


def serialize_source(habr_doc: dict) -> str:
    """Serialize a Habr doc to the compact JSON string used as ``text.source``.

    Habr's ``postForm.text.source`` is a JSON *string* (not an object), so the
    caller embeds this return value verbatim.
    """
    return json.dumps(habr_doc, ensure_ascii=False, separators=(",", ":"))


# --- Public API: preview / announce ------------------------------------------


def make_preview_doc(announce: str) -> dict:
    """Build the Habr preview (announce, «до ката») doc from caller-supplied text.

    The announce is a separate field written by the caller — never derived from
    the body. Strips, hard-caps at _PREVIEW_MAX_CHARS (word boundary), wraps in
    one inline paragraph (the postLead zone allows inline content only).
    """
    text = (announce or "").strip()
    if len(text) > _PREVIEW_MAX_CHARS:
        capped = text[:_PREVIEW_MAX_CHARS].rstrip()
        space = capped.rfind(" ")
        # Only honor the word boundary if it does not discard most of the
        # announce; otherwise keep the hard cut so a text with a single early
        # space does not collapse to a few characters.
        if space > _PREVIEW_MAX_CHARS // 2:
            capped = capped[:space].rstrip()
        text = capped
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"simple": False, "persona": False},
                "content": [{"type": "text", "text": text}],
            }
        ],
    }
