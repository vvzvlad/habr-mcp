# Habr MCP server

HTTP-only, multi-tenant MCP server for habr.com. It lets an LLM read and write on
Habr (and publish drafts) through Habr's internal, undocumented JSON API
`https://habr.com/kek/v2/`.

- **Read** works anonymously (search, feeds, article, comments).
- **Write** (comment, vote, bookmark) and the **author/draft** tools require a
  logged-in session.

The server runs over `streamable-http` and serves many users at once. There are
**no global credentials**: each user authenticates with their own bearer token
and stores their own Habr session.

## Auth flow (per user)

1. Put an opaque `Authorization: Bearer <token>` in your MCP client config — pick
   any random secret; it is just an identity key for this server.
2. Call `habr_login` once, passing the full `Cookie` header from a logged-in
   habr.com browser session (and a CSRF token where required).
3. The server stores your credentials, **encrypted at rest**, under `data/`
   (`HABR_MCP_STATE_DIR`), keyed by your token. Subsequent calls reuse them.

Reading needs no login; write/author tools report a clear guard message until you
have logged in.

## Run locally

```bash
cd /Users/vvzvlad/Data/Projects/habr-mcp
make install   # create .venv and install dev/test deps
make test      # run the test suite
make run       # start the HTTP MCP server
```

`make help` lists all targets. Config comes from ENV / `.env`
(`cp .env.example .env`, shortcut `make env`); for read-only use nothing needs to
be filled in.

## Deploy

Deploy the prebuilt image — do not build on prod. `docker-compose.yml` pulls
`ghcr.io/vvzvlad/habr-mcp:latest`, mounts a named volume on `/app/data` (the
encrypted credential store), sets `HABR_MCP_HOST=0.0.0.0` so the container binds
all interfaces, and publishes the MCP port (8765) via Traefik. MCP clients then
connect to `https://<host>/mcp`. watchtower auto-updates the container on a new
`latest`.

## Configuration

Server-level (shared, non-secret) variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `HABR_MCP_HOST` | HTTP bind address (`0.0.0.0` in Docker) | `127.0.0.1` |
| `HABR_MCP_PORT` | HTTP bind port | `8765` |
| `HABR_MCP_STATE_DIR` | Directory for the encrypted credential store | `data` |
| `HABR_LANG` | Content (`fl`) and interface (`hl`) language | `ru` |
| `HABR_X_APP_VERSION` | Value for the `x-app-version` request header | `2.329.0` |
| `PROXY` | HTTP/SOCKS proxy URL for httpx | empty |
| `REQUEST_TIMEOUT` | httpx request timeout, seconds | `20` |
| `PER_PAGE` | Page size for feeds / search | `20` |
| `DOCMOST_BASE_URL` | Base URL to download Docmost images for reupload | empty |
| `DOCMOST_API_TOKEN` | Bearer token to download Docmost attachments | empty |

Per-user Habr credentials are **not** environment variables — they arrive via
`habr_login` and live encrypted under `data/`.

## Tools

Auth / session:

| Tool | Parameters | What it does |
| --- | --- | --- |
| `habr_login` | `cookie: str`, `csrf_token: str \| None` | Save your Habr session (full browser Cookie) for your token |
| `auth_status` | — | Show your current auth state |

Read (anonymous):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `search_articles` | `query: str`, `page: int = 1` | Full-text article search |
| `list_articles` | `feed: str = "top"` (`top`/`new`/`news`), `period: str = "daily"` (`daily`/`weekly`/`monthly`/`yearly`/`alltime`), `hub: str \| None`, `page: int = 1` | Article feed |
| `get_article` | `article_id: int` | Full article text (Markdown) |
| `get_comments` | `article_id: int`, `limit: int = 100` | Comment tree |

Write (requires a session):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `post_comment` | `article_id: int`, `text: str`, `parent_id: int \| None` | Comment (0/None = top level) |
| `vote_article` | `article_id: int`, `direction: str` (`up`/`down`) | Vote on an article |
| `vote_comment` | `comment_id: int`, `direction: str` | Vote on a comment (EXPERIMENTAL) |
| `bookmark_article` | `article_id: int`, `add: bool = True` | Bookmark (removal EXPERIMENTAL) |

Author layer — drafts (requires an author session):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `create_draft` | `title: str`, `doc: str`, `hubs`, `tags`, `flow`, `format = "common"` | Create a draft from a Docmost page (`doc` = ProseMirror JSON from `get_page_json`) |
| `get_draft` | `post_id: int` | Read a draft (summary + raw ProseMirror sources) |
| `update_draft` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `format` | Update draft fields (read-modify-write autosave) |
| `delete_draft` | `post_id: int` | Delete a draft |
| `resolve_hubs` | `aliases: list[str]`, `post_id: int \| None` | Hub aliases → numeric ids |
| `list_flows` | `publication_id: int \| None` | List flows (id / alias / title) |

The author tools publish Docmost pages into Habr **drafts**. Promoting a draft to
public status ("Publish") is **not implemented** — protocol gap
(`docs/habr-publication-protocol.md` §8).

## About the write endpoints (reverse-engineering)

> Write endpoints are reverse-engineered from habr.com's internal API.

Routes are confirmed at the route level (without auth they return
`HTTP 401 Unauthenticated`):

- `post_comment` → `POST articles/<id>/comments/add/` — confirmed.
- `vote_article` → `POST articles/<id>/votes/up|down/` — confirmed.
- `bookmark_article` (add) → `POST articles/<id>/bookmarks/` — confirmed.
- `vote_comment` → `POST articles/comments/<id>/votes/up|down/` — **EXPERIMENTAL**
  (route found, not verified against a real session).
- `bookmark_article` (remove) → `DELETE articles/<id>/bookmarks/` —
  **EXPERIMENTAL**, best-effort.

If Habr changes routes, all URL/body/header logic is centralised in
`src/client.py` — fix it there.
