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
- ``make_preview_doc``       -> a minimal non-empty announce ("preview") doc.

Design notes:
- We never emit a node/mark ``type`` that is not part of the Habr schema. Unknown
  inputs degrade gracefully and (optionally) record a human-readable warning.
- Warnings are appended to the caller-provided ``warnings`` list. Some warnings
  are de-duplicated ("once" warnings) so a long document does not spam them.
"""

from __future__ import annotations

import json
from typing import Any

# --- Habr editorVersion-2 constants -----------------------------------------

# Habr renders heading level 1 -> <h2>, 2 -> <h3>, 3 -> <h4> (h1 is the article
# title), so Docmost heading levels are clamped into this 1..3 range.
_MIN_HEADING_LEVEL = 1
_MAX_HEADING_LEVEL = 3

# Approximate character budget for the preview/announce paragraph.
_PREVIEW_MAX_CHARS = 220
_PREVIEW_FALLBACK_TEXT = "Читать далее"

# Docmost mark type -> Habr mark type. Marks not listed here are either dropped
# silently (highlight/textStyle/comment) or dropped with a warning (anything
# truly unknown). ``link`` is handled specially (its attrs are rewritten).
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


# --- Public API: image collection -------------------------------------------


def collect_image_srcs(docmost_doc: dict) -> list[str]:
    """De-duplicated list of ``attrs.src`` for every Docmost ``image`` node.

    Returned in document order. Empty/missing ``src`` values are skipped.
    """
    doc = _as_doc(docmost_doc)
    srcs: list[str] = []
    seen: set[str] = set()

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "image":
            src = (node.get("attrs") or {}).get("src")
            if isinstance(src, str) and src and src not in seen:
                seen.add(src)
                srcs.append(src)
        for child in node.get("content") or []:
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
    ``comment`` are dropped silently; ``link`` keeps only its ``href``.
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
        elif mtype in _MARK_DROP_SILENT:
            continue  # keep the text, drop the mark
        else:
            _warn(warnings, f"unsupported mark dropped: {mtype}")
    return result


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
            node: dict[str, Any] = {"type": "text", "text": child.get("text", "")}
            marks = _convert_marks(child.get("marks"), warnings, seen)
            if marks:
                node["marks"] = marks
            result.append(node)
        elif ctype == "hardBreak":
            result.append({"type": "hard_break"})
        elif ctype == "mention":
            # No Habr inline-mention equivalent we can safely emit; degrade to
            # plain text using the mention label.
            label = (child.get("attrs") or {}).get("label") or "@?"
            result.append({"type": "text", "text": label})
            _warn_once(warnings, seen, "mention converted to plain text")
        elif ctype == "mathInline":
            text = (child.get("attrs") or {}).get("text") or ""
            result.append({"type": "text", "text": text})
            _warn_once(warnings, seen, "mathInline converted to plain text")
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
    for child in node.get("content") or []:
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
        return {
            "type": "paragraph",
            "attrs": {"align": align, "simple": False, "persona": False},
            "content": inline,
        }
    # An empty (e.g. trailing) paragraph carries no content key and no align.
    return {"type": "paragraph", "attrs": {"simple": False, "persona": False}}


def _build_heading(
    node: dict,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Build a Habr ``heading`` with level clamped to 1..3 (default 1)."""
    raw_level = (node.get("attrs") or {}).get("level", 1)
    try:
        level = int(raw_level)
    except (TypeError, ValueError):
        level = 1
    level = max(_MIN_HEADING_LEVEL, min(_MAX_HEADING_LEVEL, level))
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
) -> dict | None:
    """Build a Habr ``image`` node, rewriting ``src`` via ``image_url_map``.

    Returns ``None`` (and warns) if the original src has no mapped habrastorage
    URL, signalling the caller to drop the node.
    """
    attrs = node.get("attrs") or {}
    src = attrs.get("src")
    if not image_url_map or src not in image_url_map:
        _warn(warnings, f"image dropped (no habrastorage url): {src}")
        return None
    new_src = image_url_map[src]

    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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
        "content": [{"type": "image_caption"}],
    }


def _build_spoiler(title: str, children: list[dict]) -> dict:
    """Build a Habr ``spoiler`` (details/callout collapse) with a title."""
    return {"type": "spoiler", "attrs": {"title": title}, "content": children}


# --- Block dispatch ----------------------------------------------------------

# Simple wrapper nodes whose only job is to map a Docmost block name to a Habr
# block name and recurse into BLOCK children. (Lists, blockquote, list items.)
_WRAPPER_RENAME = {
    "blockquote": "blockquote",
    "bulletList": "unordered_list",
    "orderedList": "ordered_list",
    "listItem": "list_item",
    # taskList/taskItem reuse the plain list nodes; checkbox state is lost.
    "taskList": "unordered_list",
    "taskItem": "list_item",
}

# Habr block types known to be safe inside a list_item. Anything else (code_block,
# image, blockquote, spoiler, …) is kept but flagged, since the list_item schema
# is unconfirmed and the list zone may reject non-paragraph/non-list children.
_LIST_ITEM_SAFE_TYPES = {"paragraph", "unordered_list", "ordered_list"}


def _convert_blocks(
    children: Any,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a list of Docmost block nodes into a flat list of Habr blocks.

    Unknown wrappers are flattened (their block children spliced into the parent
    stream) and unknown atoms are dropped; both record a warning.
    """
    result: list[dict] = []
    if not isinstance(children, list):
        return result
    for child in children:
        if not isinstance(child, dict):
            continue
        result.extend(
            _convert_block(child, image_url_map, warnings, seen)
        )
    return result


def _convert_block(
    node: dict,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
) -> list[dict]:
    """Convert a single Docmost block node into zero or more Habr blocks.

    Returns a list so a node can expand to several blocks (e.g. a flattened
    wrapper) or to none (e.g. a dropped atom or an unmapped image).
    """
    ntype = node.get("type")

    if ntype == "paragraph":
        return [_build_paragraph(node, warnings, seen)]
    if ntype == "heading":
        return [_build_heading(node, warnings, seen)]
    if ntype == "codeBlock":
        return [_build_code_block(node)]
    if ntype == "horizontalRule":
        return [{"type": "hr", "attrs": {"inserted": True}}]

    if ntype in _WRAPPER_RENAME:
        if ntype == "taskList" or ntype == "taskItem":
            _warn_once(
                warnings, seen, "task list converted to plain list (checkbox state lost)"
            )
        inner = _convert_blocks(node.get("content"), image_url_map, warnings, seen)
        # Habr's list_item schema is unconfirmed; block content other than
        # paragraphs and nested lists may be rejected by the list zone. Keep it
        # (non-lossy) but warn once so the user knows the result may not import.
        if ntype == "listItem" or ntype == "taskItem":
            if any(
                block.get("type") not in _LIST_ITEM_SAFE_TYPES for block in inner
            ):
                _warn_once(
                    warnings,
                    seen,
                    "list item contains block content Habr may reject "
                    "(code/image/quote/etc.)",
                )
        return [{"type": _WRAPPER_RENAME[ntype], "content": inner}]

    if ntype == "callout":
        # Russian title from the callout's semantic type (info/warning/danger/
        # success), looked up case-insensitively; unknown/missing -> "Спойлер".
        callout_type = (node.get("attrs") or {}).get("type")
        key = callout_type.lower() if isinstance(callout_type, str) else ""
        title = _CALLOUT_TITLES.get(key, _CALLOUT_DEFAULT_TITLE)
        inner = _convert_blocks(node.get("content"), image_url_map, warnings, seen)
        return [_build_spoiler(title, inner)]

    if ntype == "details":
        return [_convert_details(node, image_url_map, warnings, seen)]

    if ntype == "image":
        built = _build_image(node, image_url_map, warnings)
        return [built] if built is not None else []

    # Fallback for ANY other block (table, columns, video, embed, ...): if it has
    # block children, flatten them into the parent stream so we never silently
    # lose nested content; if it is an atom, drop it. Both record a warning.
    children = node.get("content")
    if isinstance(children, list) and children:
        _warn(warnings, f"unsupported block flattened: {ntype}")
        return _convert_blocks(children, image_url_map, warnings, seen)
    _warn(warnings, f"unsupported block dropped: {ntype}")
    return []


def _convert_details(
    node: dict,
    image_url_map: dict[str, str] | None,
    warnings: list[str] | None,
    seen: set[str],
) -> dict:
    """Convert a Docmost ``details`` node into a Habr ``spoiler``.

    Title comes from the ``detailsSummary`` child's text; content comes from the
    converted block children of the ``detailsContent`` child.
    """
    summary_text = ""
    content_children: list[dict] = []
    for child in node.get("content") or []:
        if not isinstance(child, dict):
            continue
        ctype = child.get("type")
        if ctype == "detailsSummary":
            summary_text = _collect_plain_text(child)
        elif ctype == "detailsContent":
            content_children = _convert_blocks(
                child.get("content"), image_url_map, warnings, seen
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
    for child in node.get("content") or []:
        parts.append(_collect_plain_text(child))
    return "".join(parts)


# --- Public API: full document conversion ------------------------------------


def docmost_to_habr_doc(
    docmost_doc: dict,
    image_url_map: dict[str, str] | None = None,
    warnings: list[str] | None = None,
) -> dict:
    """Translate a Docmost (TipTap) ProseMirror doc into a Habr editorVersion-2 doc.

    Returns ``{"type":"doc","content":[...]}``. Never emits a non-Habr node/mark
    type. Unknown nodes/marks degrade gracefully and append a human-readable
    string to ``warnings`` (when a list is provided). ``image`` nodes get their
    ``src`` replaced via ``image_url_map``; an image whose src is not in the map
    is dropped with a warning.
    """
    doc = _as_doc(docmost_doc)
    seen: set[str] = set()
    content = _convert_blocks(doc.get("content"), image_url_map, warnings, seen)
    return {"type": "doc", "content": content}


# --- Public API: serialization -----------------------------------------------


def serialize_source(habr_doc: dict) -> str:
    """Serialize a Habr doc to the compact JSON string used as ``text.source``.

    Habr's ``postForm.text.source`` is a JSON *string* (not an object), so the
    caller embeds this return value verbatim.
    """
    return json.dumps(habr_doc, ensure_ascii=False, separators=(",", ":"))


# --- Public API: preview / announce ------------------------------------------


def make_preview_doc(habr_doc: dict) -> dict:
    """Build a minimal NON-EMPTY Habr 'preview' (announce) doc.

    The preview zone allows only inline content, so we emit exactly one
    paragraph. Its text is the concatenated text of the first paragraph/heading
    found in ``habr_doc``, trimmed to ~220 chars. If no text is found, fall back
    to a paragraph reading 'Читать далее'.
    """
    text = ""
    for block in (habr_doc.get("content") or []):
        if not isinstance(block, dict):
            continue
        if block.get("type") in ("paragraph", "heading"):
            candidate = _collect_plain_text(block).strip()
            if candidate:
                text = candidate
                break

    if not text:
        text = _PREVIEW_FALLBACK_TEXT
    if len(text) > _PREVIEW_MAX_CHARS:
        text = text[:_PREVIEW_MAX_CHARS].rstrip()

    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "attrs": {"align": None, "simple": False, "persona": False},
                "content": [{"type": "text", "text": text}],
            }
        ],
    }
