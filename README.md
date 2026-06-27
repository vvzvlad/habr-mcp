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
| `HABR_COOKIE` | Полный Cookie-заголовок браузера для авторских инструментов (черновики) | пусто |
| `HABR_USER_UUID` | Значение заголовка `habr-user-uuid` | пусто |
| `HABR_X_APP_VERSION` | Значение заголовка `x-app-version` | `2.329.0` |
| `DOCMOST_BASE_URL` | База для скачивания картинок Docmost (перезалив) | пусто |
| `DOCMOST_API_TOKEN` | Bearer-токен для скачивания вложений Docmost | пусто |
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

Авторский слой — черновики (требует авторской сессии: `HABR_COOKIE` + `HABR_CSRF_TOKEN`):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `create_draft` | `title: str`, `doc: str`, `hubs`, `tags`, `flow`, `format = "common"` | Создать черновик из страницы Docmost (`doc` = ProseMirror-JSON из `get_page_json`) |
| `get_draft` | `post_id: int` | Прочитать черновик (сводка + сырые ProseMirror-исходники) |
| `update_draft` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `format` | Обновить поля черновика (read-modify-write автосейв) |
| `delete_draft` | `post_id: int` | Удалить черновик |
| `resolve_hubs` | `aliases: list[str]`, `post_id: int \| None` | Алиасы хабов → числовые id |
| `list_flows` | `publication_id: int \| None` | Список потоков (id / alias / title) |

Авторские инструменты публикуют страницы Docmost в **черновики** Хабра. Тело статьи
конвертируется из ProseMirror Docmost в дерево Habr editorVersion-2; картинки
скачиваются из Docmost (`DOCMOST_BASE_URL` + `DOCMOST_API_TOKEN`) и перезаливаются
на habrastorage (сбой картинки не прерывает публикацию — текст уходит, картинка
выбрасывается с предупреждением). Перевод черновика в публичный статус
(«Опубликовать») **не реализован** — пробел протокола (`docs/habr-publication-protocol.md` §8).

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
