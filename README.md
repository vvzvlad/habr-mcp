# Habr MCP server

MCP-сервер (stdio) для habr.com. Даёт LLM возможность читать и писать на Хабре
через внутренний (недокументированный) JSON API `https://habr.com/kek/v2/`.

- **Чтение** работает анонимно (поиск, ленты, статья, комментарии).
- **Запись** (комментарий, голос, закладка) требует залогиненной сессии:
  cookie `connect.sid` + CSRF-токен, передаются через переменные окружения.

## Установка

```bash
cd /Users/vvzvlad/Data/Projects/habr-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

Для прод-запуска без тестов достаточно `requirements.txt`.

## Конфигурация

Скопируйте `.env.example` в `.env` и при необходимости заполните значения.
Для чтения ничего заполнять не нужно.

Переменные:

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `HABR_LANG` | Язык контента (`fl`) и интерфейса (`hl`) | `ru` |
| `HABR_CONNECT_SID` | Значение cookie `connect.sid` (для записи) | пусто |
| `HABR_CSRF_TOKEN` | CSRF-токен (для записи) | пусто |
| `HABR_CSRF_COOKIE_NAME` | Имя CSRF-cookie (double-submit) | `csrf_token` |
| `PROXY` | HTTP/SOCKS прокси для httpx | пусто |
| `REQUEST_TIMEOUT` | Таймаут запроса, сек | `20` |
| `PER_PAGE` | Размер страницы лент/поиска | `20` |

### Где взять `connect.sid` и CSRF-токен

Залогиньтесь на habr.com в браузере, затем откройте DevTools:

- **`HABR_CONNECT_SID`**: вкладка *Application* → *Cookies* → `https://habr.com`
  → скопируйте значение cookie `connect.sid`.
- **`HABR_CSRF_TOKEN`**: вкладка *Network* → выполните любое действие записи
  (например, поставьте плюс статье) → откройте запрос → *Request Headers* →
  скопируйте значение заголовка `csrf-token` (оно совпадает со значением cookie
  `csrf_token`).

## Запуск тестов

```bash
.venv/bin/pytest -q
```

## Подключение к Claude Code / Claude Desktop

Зарегистрируйте сервер как stdio MCP в конфиге клиента:

```json
{
  "mcpServers": {
    "habr": {
      "command": "/Users/vvzvlad/Data/Projects/habr-mcp/.venv/bin/python",
      "args": ["main.py"],
      "cwd": "/Users/vvzvlad/Data/Projects/habr-mcp",
      "env": {
        "HABR_LANG": "ru",
        "HABR_CONNECT_SID": "",
        "HABR_CSRF_TOKEN": "",
        "HABR_CSRF_COOKIE_NAME": "csrf_token"
      }
    }
  }
}
```

`HABR_CONNECT_SID` / `HABR_CSRF_TOKEN` можно оставить пустыми для режима только
для чтения.

## Инструменты

Чтение (анонимно):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `search_articles` | `query: str`, `page: int = 1` | Поиск статей по тексту |
| `list_articles` | `feed: str = "top"` (`top`/`new`/`news`), `period: str = "daily"` (`daily`/`weekly`/`monthly`/`yearly`/`alltime`), `hub: str \| None`, `page: int = 1` | Лента статей |
| `get_article` | `article_id: int` | Полный текст статьи (Markdown) |
| `get_comments` | `article_id: int`, `limit: int = 100` | Дерево комментариев |

Запись (требует сессии):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `post_comment` | `article_id: int`, `text: str`, `parent_id: int \| None` | Комментарий (0/None = верхний уровень) |
| `vote_article` | `article_id: int`, `direction: str` (`up`/`down`) | Голос за статью |
| `vote_comment` | `comment_id: int`, `direction: str` | Голос за комментарий (ЭКСПЕРИМЕНТАЛЬНО) |
| `bookmark_article` | `article_id: int`, `add: bool = True` | Закладка (удаление ЭКСПЕРИМЕНТАЛЬНО) |

## Про write-эндпоинты (reverse-engineering)

> Write endpoints are reverse-engineered from habr.com's internal API.

Маршруты записи получены реверс-инжинирингом внутреннего API Хабра и подтверждены
на уровне маршрута (без авторизации возвращают `HTTP 401 Unauthenticated`):

- `post_comment` → `POST articles/<id>/comments/add/` — подтверждён.
- `vote_article` → `POST articles/<id>/votes/up|down/` — подтверждён.
- `bookmark_article` (добавление) → `POST articles/<id>/bookmarks/` — подтверждён.
- `vote_comment` → `POST articles/comments/<id>/votes/up|down/` —
  **ЭКСПЕРИМЕНТАЛЬНО**: маршрут найден, но не проверен с реальной сессией.
- `bookmark_article` (удаление) → `DELETE articles/<id>/bookmarks/` —
  **ЭКСПЕРИМЕНТАЛЬНО**, best-effort.

Если Хабр поменяет маршруты — вся логика URL/тел/заголовков централизована в
`src/client.py`, правьте там.
