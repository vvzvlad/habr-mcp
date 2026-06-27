"""Pure formatting helpers: HTML conversion and compact, LLM-friendly renders.

These functions take the parsed Habr payloads (dicts) and produce plain Russian
text / Markdown strings. No I/O here, so they are trivially unit-testable.
"""

from __future__ import annotations

import re
from typing import Any

from markdownify import markdownify

# Collapse 3+ consecutive newlines down to a double newline.
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
# Any run of whitespace (incl. newlines) for plain-text flattening.
_WS_RE = re.compile(r"\s+")
# Markdown emphasis/heading/list markers to strip for plain-text output.
_MD_MARKERS_RE = re.compile(r"[*_`#>]+")


def html_to_markdown(html: str) -> str:
    """Convert an HTML body to Markdown (ATX headings); tidy blank lines."""
    if not html:
        return ""
    md = markdownify(html, heading_style="ATX")
    md = _MULTI_BLANK_RE.sub("\n\n", md)
    return md.strip()


def html_to_text(html: str) -> str:
    """Convert HTML to a single-line-ish plain text (titles, comments)."""
    if not html:
        return ""
    # markdownify strips tags; then drop markdown emphasis/heading markers and
    # flatten all whitespace to single spaces.
    text = markdownify(html)
    text = _MD_MARKERS_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def _truncate(text: str, limit: int = 280) -> str:
    """Shorten ``text`` to ``limit`` chars with an ellipsis if needed."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _date_part(iso: str | None) -> str:
    """Return just the ``YYYY-MM-DD`` part of an ISO timestamp."""
    if not iso:
        return "?"
    return iso.split("T", 1)[0]


def _author_alias(author: Any) -> str:
    """Pull an author's alias (login) from an author dict, robustly."""
    if isinstance(author, dict):
        return author.get("alias") or author.get("fullname") or "?"
    return "?"


def format_article_list(payload: dict[str, Any], header: str) -> str:
    """Render a feed/search payload as a numbered list, in publicationIds order."""
    ids = payload.get("publicationIds") or []
    refs = payload.get("publicationRefs") or {}
    pages_count = payload.get("pagesCount")

    lines: list[str] = [header]
    if pages_count is not None:
        lines.append(f"Всего страниц: {pages_count}")
    if not ids:
        lines.append("Ничего не найдено.")
        return "\n".join(lines)

    lines.append("")
    for index, article_id in enumerate(ids, start=1):
        ref = refs.get(str(article_id)) or refs.get(article_id) or {}
        title = html_to_text(ref.get("titleHtml", "")) or "(без заголовка)"
        author = _author_alias(ref.get("author"))
        date = _date_part(ref.get("timePublished"))
        stats = ref.get("statistics") or {}
        score = stats.get("score", "?")
        comments = stats.get("commentsCount", "?")
        reading = ref.get("readingTime")
        reading_str = f"{reading} мин" if reading is not None else "?"
        hubs = ref.get("hubs") or []
        hub_titles = ", ".join(
            html_to_text(h.get("titleHtml") or h.get("title") or "")
            for h in hubs[:3]
            if isinstance(h, dict)
        )
        lines.append(
            f"{index}. {title}\n"
            f"   id={article_id} · @{author} · {date} · "
            f"рейтинг {score} · комментариев {comments} · чтение {reading_str}"
        )
        if hub_titles:
            lines.append(f"   хабы: {hub_titles}")
    return "\n".join(lines)


def format_article(data: dict[str, Any]) -> str:
    """Render a full article: metadata block, separator, Markdown body."""
    article_id = data.get("id", "?")
    title = html_to_text(data.get("titleHtml", "")) or "(без заголовка)"
    author = _author_alias(data.get("author"))
    date = _date_part(data.get("timePublished"))
    stats = data.get("statistics") or {}
    score = stats.get("score", "?")
    votes_plus = stats.get("votesCountPlus", "?")
    votes_minus = stats.get("votesCountMinus", "?")
    reading = data.get("readingTime")
    reading_str = f"{reading} мин" if reading is not None else "?"
    complexity = data.get("complexity") or "—"
    hubs = data.get("hubs") or []
    hub_titles = ", ".join(
        html_to_text(h.get("titleHtml") or h.get("title") or "")
        for h in hubs
        if isinstance(h, dict)
    )
    tags = data.get("tags") or []
    tag_titles = ", ".join(
        html_to_text(t.get("titleHtml", "")) for t in tags if isinstance(t, dict)
    )
    url = f"https://habr.com/ru/articles/{article_id}/"

    meta = [
        f"# {title}",
        f"id: {article_id}",
        f"автор: @{author}",
        f"дата: {date}",
        f"рейтинг: {score} (+{votes_plus} / -{votes_minus})",
        f"время чтения: {reading_str}",
        f"сложность: {complexity}",
    ]
    if hub_titles:
        meta.append(f"хабы: {hub_titles}")
    if tag_titles:
        meta.append(f"теги: {tag_titles}")
    meta.append(f"url: {url}")

    body = html_to_markdown(data.get("textHtml", ""))
    return "\n".join(meta) + "\n\n---\n\n" + body


def _render_comment(
    comment_id: str,
    comments: dict[str, Any],
    out: list[str],
    counter: list[int],
    limit: int,
    seen: set[str],
) -> None:
    """Recursively render one comment and its children into ``out``.

    ``counter`` is a one-element list used as a mutable shared counter so we can
    stop once ``limit`` comments have been emitted. ``seen`` tracks already
    rendered comment ids so malformed/cyclic ``children`` graphs cannot cause a
    node to be rendered twice or send us into an infinite loop; it also bounds
    recursion depth by ``limit`` against pathologically deep chains.
    """
    if counter[0] >= limit:
        return
    comment = comments.get(str(comment_id)) or comments.get(comment_id)
    if not isinstance(comment, dict):
        return
    # Skip ids we have already rendered (cycle/duplicate protection).
    key = str(comment.get("id", comment_id))
    if key in seen:
        return
    seen.add(key)
    counter[0] += 1

    # Habr can return numeric fields as strings; coerce ``level`` safely so a
    # value like "1" does not blow up the indent multiplication.
    try:
        level = int(comment.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    indent = "  " * level
    author = _author_alias(comment.get("author"))
    date = _date_part(comment.get("timePublished"))
    score = comment.get("score", 0)
    deleted = comment.get("deleted") or comment.get("status") == "deleted"
    if deleted:
        body = "[удалён]"
    else:
        body = _truncate(html_to_text(comment.get("message", "")))

    out.append(f"{indent}— @{author} · {date} · рейтинг {score}")
    out.append(f"{indent}  {body}")

    for child_id in comment.get("children") or []:
        if counter[0] >= limit:
            break
        _render_comment(child_id, comments, out, counter, limit, seen)


def format_draft(payload: dict[str, Any]) -> str:
    """Render a draft (``post-data`` response) as a compact Russian summary.

    Appends the RAW serialized ``text`` and ``preview`` sources so an LLM can
    round-trip them. All ``.get`` chains are guarded against missing/None.
    """
    form = payload.get("postForm") or payload
    if not isinstance(form, dict):
        form = {}

    def _list(value: Any) -> str:
        if isinstance(value, list) and value:
            return ", ".join(str(item) for item in value)
        return "—"

    meta = [
        f"id: {form.get('id', '?')}",
        f"заголовок: {form.get('title') or '(без заголовка)'}",
        f"статус: {form.get('status') or '—'}",
        f"язык: {form.get('lang') or '—'}",
        f"тип: {form.get('type') or '—'}",
        f"поток (flow): {form.get('flow') or '—'}",
        f"формат: {form.get('format') or '—'}",
        f"сложность: {form.get('complexity') or '—'}",
        f"хабы: {_list(form.get('hubs'))}",
        f"теги: {_list(form.get('tags'))}",
        f"опубликовано: {form.get('publishedAt') or '—'}",
    ]

    text_block = form.get("text") if isinstance(form.get("text"), dict) else {}
    preview_block = form.get("preview") if isinstance(form.get("preview"), dict) else {}
    text_source = text_block.get("source") or ""
    preview_source = preview_block.get("source") or ""

    return (
        "\n".join(meta)
        + "\n\n--- TEXT (ProseMirror source) ---\n"
        + text_source
        + "\n\n--- PREVIEW (ProseMirror source) ---\n"
        + preview_source
    )


def format_comments(payload: dict[str, Any], limit: int) -> str:
    """Render the comment tree from ``threads`` order, indented by level."""
    comments = payload.get("comments") or {}
    threads = payload.get("threads") or []
    total = len(comments)

    if not comments:
        return "Комментариев нет."

    out: list[str] = []
    counter = [0]
    seen: set[str] = set()
    for root_id in threads:
        if counter[0] >= limit:
            break
        _render_comment(root_id, comments, out, counter, limit, seen)

    # The rendered count can never legitimately exceed the number of distinct
    # comments; clamp it so a cyclic graph cannot print "показано 100 из 2".
    rendered = counter[0]
    shown = min(rendered, total)
    header = f"Комментарии (показано {shown} из {total}):"
    if total > rendered:
        out.append("")
        out.append(f"… показаны первые {shown} из {total} комментариев")
    return header + "\n\n" + "\n".join(out)
