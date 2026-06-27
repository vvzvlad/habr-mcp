# AGENTS

Онбординг для агентов и людей, работающих с проектом.

## Структура

- `main.py` — тонкая точка входа: собирает сервер и запускает stdio.
- `src/settings.py` — конфигурация (pydantic-settings, из ENV / `.env`).
- `src/client.py` — async HTTP-клиент к Habr `kek/v2` API. Вся специфика
  маршрутов/тел/заголовков централизована здесь.
- `src/formatting.py` — чистые функции форматирования (HTML→Markdown/текст,
  рендер списков/статьи/комментариев/черновика).
- `src/converter.py` — чистый конвертер Docmost (TipTap) ProseMirror →
  Habr editorVersion-2 (для авторских инструментов).
- `src/server.py` — `build_server()`: регистрирует MCP-инструменты (чтение,
  запись-комментарии/голоса, авторский слой черновиков).
- `tests/` — тесты на pytest (httpx мокается через respx).

## Авторский слой (черновики)

Инструменты `create_draft` / `get_draft` / `update_draft` / `delete_draft` /
`resolve_hubs` / `list_flows` публикуют страницы Docmost в **черновики** Хабра
(`publication/…`, протокол в `docs/habr-publication-protocol.md`). Перевод
черновика в публичный статус («Опубликовать») **не реализован** — пробел протокола §8.

Авторская авторизация отличается от записи комментариев: нужен `HABR_COOKIE`
(полный Cookie-заголовок браузера: `connect_sid` + `hsec_id` + `habrsession_id` + …)
и `HABR_CSRF_TOKEN`. Тело статьи конвертируется из ProseMirror Docmost в дерево
Habr editorVersion-2 (`src/converter.py`). Картинки сначала скачиваются из Docmost
(`DOCMOST_BASE_URL` + `DOCMOST_API_TOKEN`) и перезаливаются на habrastorage
(`publication/upload`, ЭКСПЕРИМЕНТАЛЬНО) — сбой картинки не прерывает публикацию
(текст уходит, нерезолвленные картинки выбрасываются конвертером с предупреждением).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # затем при необходимости заполнить креды записи
```

## Running tests

```bash
.venv/bin/pytest
```

## Conventions

- Конфигурация — только из ENV / `.env` через `src/settings.py` (pydantic-settings).
- Креды записи (`HABR_CONNECT_SID`, `HABR_CSRF_TOKEN`) живут только в `.env` —
  никаких дефолтных кред в коде.
- Чтение работает анонимно; запись требует сессии и аккуратно сообщает об ошибке,
  если кред нет.
- Все комментарии в коде — на английском.
- Описания MCP-инструментов (`description=`) — на русском (текст для LLM).
- Тесты обязательны для нового кода.
- Write-эндпоинты — reverse-engineered из внутреннего API Хабра; маршруты правятся
  централизованно в `src/client.py`.
