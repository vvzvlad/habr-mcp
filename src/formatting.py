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


def format_drafts_list(payload: dict[str, Any], header: str) -> str:
    """Render an ``articles/drafts`` payload as a numbered, draft-focused list.

    Same ``publicationIds`` / ``publicationRefs`` shape as a feed, but shows the
    post id (for get_draft / update_draft_from_docmost / delete_draft), title,
    flow, hubs and tags instead of feed score/date (a draft has neither).
    """
    ids = payload.get("publicationIds") or []
    refs = payload.get("publicationRefs") or {}
    pages_count = payload.get("pagesCount")

    lines: list[str] = [header]
    if pages_count is not None:
        lines.append(f"Всего страниц: {pages_count}")
    if not ids:
        lines.append("Черновиков нет.")
        return "\n".join(lines)

    lines.append("")
    for index, draft_id in enumerate(ids, start=1):
        ref = refs.get(str(draft_id)) or refs.get(draft_id) or {}
        title = html_to_text(ref.get("titleHtml") or "") or "(без заголовка)"
        reading = ref.get("readingTime")
        reading_str = f"{reading} мин" if reading is not None else "?"
        flow = ref.get("flowNew")
        if isinstance(flow, dict):
            flow_str = flow.get("alias") or flow.get("id") or "—"
        else:
            flow_str = "—"
        hubs = ref.get("hubs") or []
        hub_titles = ", ".join(
            html_to_text(h.get("titleHtml") or h.get("title") or "")
            for h in hubs[:3]
            if isinstance(h, dict)
        )
        tags = ref.get("tags") or []
        tag_titles = ", ".join(
            html_to_text(t.get("titleHtml") or "") if isinstance(t, dict) else str(t)
            for t in tags[:5]
        )
        lines.append(
            f"{index}. {title}\n   id={draft_id} · поток {flow_str} · чтение {reading_str}"
        )
        if hub_titles:
            lines.append(f"   хабы: {hub_titles}")
        if tag_titles:
            lines.append(f"   теги: {tag_titles}")
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
