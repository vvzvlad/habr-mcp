# MCP-сервер для Habr

[English](README.md) | **Русский**

Многопользовательский MCP-сервер для habr.com, работающий только по HTTP. Позволяет
LLM читать и писать на Habr (и публиковать черновики) через внутренний
недокументированный JSON-API Habr `https://habr.com/kek/v2/`.

- **Чтение** работает анонимно (поиск, ленты, статья, комментарии).
- **Запись** (комментарии, голоса, закладки) и инструменты **автора/черновиков**
  требуют авторизованной сессии.

Сервер работает по `streamable-http` и обслуживает много пользователей одновременно.
**Глобальных учётных данных нет**: каждый пользователь аутентифицируется собственным
bearer-токеном и хранит свою сессию Habr.

## Поток авторизации (для каждого пользователя)

1. Укажите произвольный `Authorization: Bearer <token>` в конфиге вашего
   MCP-клиента — это любой случайный секрет, он служит лишь ключом идентификации
   для этого сервера.
2. Один раз вызовите `habr_login`, передав полный заголовок `Cookie` из
   авторизованной браузерной сессии habr.com (и CSRF-токен, где требуется).
3. Сервер сохраняет ваши учётные данные **в зашифрованном виде на диске**, в
   каталоге `data/` (`HABR_MCP_STATE_DIR`), по ключу вашего токена. Последующие
   вызовы переиспользуют их.

Для чтения вход не нужен; инструменты записи/автора возвращают понятное
предупреждение, пока вы не авторизуетесь.

## Локальный запуск

```bash
cd /Users/vvzvlad/Data/Projects/habr-mcp
make install   # создать .venv и установить dev/test-зависимости
make test      # запустить набор тестов
make run       # запустить HTTP MCP-сервер
```

`make help` выводит все цели (targets) Makefile. Конфигурация берётся из ENV / `.env`
(`cp .env.example .env`, ярлык `make env`); для режима «только чтение» заполнять
ничего не нужно.

## Деплой

Деплойте готовый образ — не собирайте на проде. `docker-compose.yml` тянет
`ghcr.io/vvzvlad/habr-mcp:latest`, монтирует именованный том в `/app/data`
(зашифрованное хранилище учётных данных), задаёт `HABR_MCP_HOST=0.0.0.0`, чтобы
контейнер слушал все интерфейсы, и публикует порт MCP (8765) через Traefik.
MCP-клиенты подключаются к `https://<host>/mcp`. watchtower автоматически обновляет
контейнер при выходе нового `latest`.

## Конфигурация

Переменные уровня сервера (общие, несекретные):

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `HABR_MCP_HOST` | Адрес привязки HTTP (`0.0.0.0` в Docker) | `127.0.0.1` |
| `HABR_MCP_PORT` | Порт привязки HTTP | `8765` |
| `HABR_MCP_STATE_DIR` | Каталог зашифрованного хранилища учётных данных | `data` |
| `HABR_LANG` | Язык контента (`fl`) и интерфейса (`hl`) | `ru` |
| `HABR_X_APP_VERSION` | Значение заголовка запроса `x-app-version` | `2.329.0` |
| `PROXY` | URL HTTP/SOCKS-прокси для httpx | пусто |
| `REQUEST_TIMEOUT` | Тайм-аут запроса httpx, секунды | `20` |
| `PER_PAGE` | Размер страницы для лент / поиска | `20` |
| `HABR_MCP_ENABLE_SOCIAL_TOOLS` | Включить «социальные» инструменты (поиск/ленты/комментарии/голоса); по умолчанию выключено | `false` |

Учётные данные Habr для каждого пользователя **не** являются переменными
окружения — они поступают через `habr_login` и хранятся в зашифрованном виде в
`data/`.

## Инструменты

Авторизация / сессия:

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `habr_login` | `cookie: str` | Сохранить вашу сессию Habr (полный браузерный Cookie) под ваш токен; csrf-токен определяется автоматически |
| `auth_status` | — | Показать ваше текущее состояние авторизации |

Чтение (анонимно):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `search_articles` | `query: str`, `page: int = 1` | Полнотекстовый поиск статей |
| `list_articles` | `feed: str = "top"` (`top`/`new`/`news`), `period: str = "daily"` (`daily`/`weekly`/`monthly`/`yearly`/`alltime`), `hub: str \| None`, `page: int = 1` | Лента статей |
| `get_article` | `article_id: int` | Полный текст статьи (Markdown) |
| `get_comments` | `article_id: int`, `limit: int = 100` | Дерево комментариев |

> Инструменты `search_articles`, `list_articles` и `get_comments` — **социальные**
> и **по умолчанию выключены**. Включите их (вместе с инструментами записи ниже)
> через `HABR_MCP_ENABLE_SOCIAL_TOOLS=true`. `get_article` доступен всегда.

Запись (требует сессии):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `post_comment` | `article_id: int`, `text: str`, `parent_id: int \| None` | Комментарий (0/None — верхний уровень) |
| `vote_article` | `article_id: int`, `direction: str` (`up`/`down`) | Голос за статью |
| `vote_comment` | `comment_id: int`, `direction: str` | Голос за комментарий (ЭКСПЕРИМЕНТАЛЬНО) |

> `post_comment`, `vote_article` и `vote_comment` — **социальные инструменты**,
> **по умолчанию выключены**; включаются через `HABR_MCP_ENABLE_SOCIAL_TOOLS=true`.

Слой автора — черновики (требует сессии автора):

| Инструмент | Параметры | Что делает |
| --- | --- | --- |
| `create_draft_from_docmost` | `title: str`, `doc: str \| dict`, `hubs`, `tags`, `flow`, `format = "common"` | Создать черновик из страницы Docmost (`doc` — ProseMirror-JSON из `get_page_json`, инлайн **или** MCP `resource_link` на него) |
| `create_draft_from_gdoc` | `title: str`, `doc: str \| dict`, `hubs`, `tags`, `flow`, `format = "common"` | Создать черновик из документа Google Docs (`doc` — JSON из `readDocument(format='json')`, инлайн **или** MCP `resource_link` на него) |
| `get_draft` | `post_id: int` | Прочитать черновик (сводка + сырые ProseMirror-исходники) |
| `list_drafts` | `page: int = 1` | Список черновиков текущего автора (id, заголовок, поток, хабы, теги) |
| `update_draft_from_docmost` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `format` | Обновить поля черновика (автосейв read-modify-write) |
| `update_draft_from_gdoc` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `format` | Обновить поля черновика из документа Google Docs (`doc` — JSON из `readDocument(format='json')`) |
| `delete_draft` | `post_id: int` | Удалить черновик |
| `resolve_hubs` | `aliases: list[str]`, `post_id: int \| None` | Алиасы хабов → числовые id |
| `list_flows` | `publication_id: int \| None` | Список потоков (id / алиас / название) |

Инструменты автора публикуют страницы Docmost **и документы Google Docs** в
**черновики** Habr. Инструменты `*_from_gdoc` сначала конвертируют JSON из Google
Docs API в промежуточное дерево в формате Docmost (TipTap) (`src/gdoc_converter.py`),
а затем переиспользуют тот же конвейер Docmost → Habr (картинки, разметка, таблицы,
списки, превью). Перевод черновика в публичный статус («Publish») **не реализован** —
пробел в протоколе (`docs/habr-publication-protocol.md` §8).

### Приём контента: инлайн или `resource_link`

Тело `doc` и картинки документа можно передать как **инлайн** (как раньше), так и в
виде MCP **`resource_link`** (`{"type":"resource_link","uri":...}`). Для ссылки habr
сам забирает `uri` — обычным анонимным HTTP GET (без учётных данных) либо локальным
декодированием для `data:`-URI. Инлайн позволяет клиенту отправить большое тело прямо
в запрос через `curl`, не прогоняя его через модель. Картинки (только узлы `image`)
разрешаются так же и перезаливаются на habrastorage; если источник отдаёт `ETag` в
виде sha256, скачанные байты проверяются на целостность. **Связки с Docmost больше
нет** — прежний путь скачивания картинок через `DOCMOST_BASE_URL` /
`DOCMOST_API_TOKEN` убран; habr забирает любой переданный ему URL/ссылку без токена.
Полный контракт продьюсера/консьюмера — в `docs/resource-link-contract.md`.

## О write-эндпоинтах (реверс-инжиниринг)

> Write-эндпоинты получены реверс-инжинирингом внутреннего API habr.com.

Маршруты подтверждены на уровне роутов (без авторизации возвращают
`HTTP 401 Unauthenticated`):

- `post_comment` → `POST articles/<id>/comments/add/` — подтверждён.
- `vote_article` → `POST articles/<id>/votes/up|down/` — подтверждён.
- `bookmark_article` (добавление) → `POST articles/<id>/bookmarks/` — подтверждён.
- `vote_comment` → `POST articles/comments/<id>/votes/up|down/` —
  **ЭКСПЕРИМЕНТАЛЬНО** (маршрут найден, не проверен на реальной сессии).
- `bookmark_article` (удаление) → `DELETE articles/<id>/bookmarks/` —
  **ЭКСПЕРИМЕНТАЛЬНО**, по возможности.

Если Habr изменит маршруты — вся логика URL/тел/заголовков сосредоточена в
`src/client.py`, правьте там.
