# AGENTS

Онбординг для агентов и людей, работающих с проектом.

## Структура

- `main.py` — тонкая точка входа: собирает сервер и запускает stdio.
- `src/settings.py` — конфигурация (pydantic-settings, из ENV / `.env`).
- `src/client.py` — async HTTP-клиент к Habr `kek/v2` API. Вся специфика
  маршрутов/тел/заголовков централизована здесь.
- `src/formatting.py` — чистые функции форматирования (HTML→Markdown/текст,
  рендер списков/статьи/комментариев).
- `src/server.py` — `build_server()`: регистрирует 8 MCP-инструментов.
- `tests/` — тесты на pytest (httpx мокается через respx).

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
