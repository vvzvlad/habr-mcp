# Habr MCP server

**English** | [Русский](README.ru.md)

HTTP-only, multi-tenant MCP server for habr.com. It lets an LLM read and write on
Habr (and publish drafts) through Habr's internal, undocumented JSON API
`https://habr.com/kek/v2/`.

- **Read** works anonymously (a single article).
- The **author/draft** tools require a logged-in session.

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
| `PROXY` | HTTP/SOCKS proxy URL for httpx | empty |
| `REQUEST_TIMEOUT` | httpx request timeout, seconds | `20` |
| `PER_PAGE` | Page size for feeds / search | `20` |

Per-user Habr credentials are **not** environment variables — they arrive via
`habr_login` and live encrypted under `data/`.

## Tools

Auth / session:

| Tool | Parameters | What it does |
| --- | --- | --- |
| `habr_login` | `cookie: str` | Save your Habr session (full browser Cookie) for your token; the csrf token is auto-detected |
| `auth_status` | — | Show your current auth state |
| `whoami` | — | Show which Habr account you are logged in as (login, name, id, profile link) |

Read (anonymous):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `get_article` | `article_id: int` | Full article text (Markdown) |

Author layer — drafts (requires an author session):

| Tool | Parameters | What it does |
| --- | --- | --- |
| `create_draft_from_docmost` | `title: str`, `doc: str \| dict`, `hubs`, `tags`, `flow`, `announce`, `format = "common"` | `announce` = required teaser (100–3000 chars); Create a draft from a Docmost page (`doc` = ProseMirror JSON from `get_page_json`, inline **or** an MCP `resource_link` to it) |
| `create_draft_from_gdoc` | `title: str`, `doc: str \| dict`, `hubs`, `tags`, `flow`, `announce`, `format = "common"` | `announce` = required teaser (100–3000 chars); Create a draft from a Google Docs document (`doc` = JSON from `readDocument(format='json')`, inline **or** an MCP `resource_link` to it) |
| `get_draft` | `post_id: int` | Read a draft (summary + raw ProseMirror sources) |
| `list_drafts` | `page: int = 1` | List the logged-in author's drafts (id, title, flow, hubs, tags) |
| `update_draft_from_docmost` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `announce`, `format` | Update draft fields (read-modify-write autosave) (`announce` optional) |
| `update_draft_from_gdoc` | `post_id: int`, `title`, `doc`, `hubs`, `tags`, `flow`, `announce`, `format` | Update draft fields from a Google Docs document (`doc` = JSON from `readDocument(format='json')`) (`announce` optional) |
| `delete_draft` | `post_id: int` | Delete a draft |
| `resolve_hubs` | `aliases: list[str]`, `post_id: int \| None` | Hub aliases → numeric ids |
| `search_hubs` | `query: str = ""`, `limit: int = 40` | Search hubs by substring (returns `id  alias  title`); pass an id to `hubs` |
| `list_flows` | `publication_id: int \| None` | List flows (id / alias / title) |

The author tools publish Docmost pages **and Google Docs documents** into Habr
**drafts**. The `*_from_gdoc` tools first convert the Google Docs API JSON into an
intermediate Docmost-shaped (TipTap) tree (`src/gdoc_converter.py`), then reuse
the same Docmost → Habr pipeline (images, marks, tables, lists, previews).
Promoting a draft to public status ("Publish") is **not implemented** — protocol
gap (`docs/habr-publication-protocol.md` §8).

### Content intake: inline or `resource_link`

The `doc` body and the document's images can each be passed **inline** (as before)
or as an MCP **`resource_link`** (`{"type":"resource_link","uri":...}`). For a link,
habr fetches the `uri` itself — a plain anonymous HTTP GET (no credentials), or a
local decode for a `data:` URI. Inline lets a client `curl` a large body straight
into the request without routing it through the model. Images (only `image` nodes)
are resolved the same way and re-hosted on habrastorage; when the source returns a
sha256-shaped `ETag`, the fetched bytes are integrity-checked. There is **no
Docmost coupling** anymore — the old `DOCMOST_BASE_URL` / `DOCMOST_API_TOKEN`
image-download path is gone; habr fetches whatever URL/link it is given, with no
token. See `docs/resource-link-contract.md` for the full producer/consumer contract.
