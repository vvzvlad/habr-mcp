# Agent Instructions — habr-mcp

HTTP-only, multi-tenant MCP server for habr.com (read + write + author/draft
tools). It talks to Habr's internal (undocumented) JSON API
`https://habr.com/kek/v2/`.

## Project structure
- `main.py` — thin entry point: build the server and run it over `streamable-http`.
- `src/settings.py` — configuration (pydantic-settings, from ENV / `.env`);
  exposes the `settings` singleton built via `load_settings_or_exit`.
- `src/config_errors.py` — turns a pydantic `ValidationError` into a clear
  startup message naming the missing/invalid env var (shared startup helper).
- `src/client.py` — async HTTP client for Habr `kek/v2`. All route/body/header
  specifics are centralised here.
- `src/store.py` — per-token credential store (`creds.json`) written under the
  state dir; credentials are encrypted at rest.
- `src/registry.py` — builds one `HabrClient` per bearer token from the store.
- `src/formatting.py` — pure formatting helpers (HTML → Markdown/text, rendering
  of feeds/articles/comments/drafts).
- `src/converter.py` — pure Docmost (TipTap) ProseMirror → Habr editorVersion-2
  converter (for the author/draft tools).
- `src/gdoc_converter.py` — pure Google Docs API "Document" JSON → intermediate
  Docmost-shaped (TipTap) doc; its output then feeds `src/converter.py`.
- `src/server.py` — `build_server()`: registers MCP tools (read, write
  comments/votes, the author/draft layer).
- `tests/` — pytest (httpx is mocked via respx).
- `data/` — runtime state (the encrypted credential store); gitignored, mounted
  as a docker volume.

## Identity & multi-tenancy
The server has NO global Habr credentials. Each user puts an opaque
`Authorization: Bearer <token>` in their MCP client config and calls
`habr_login` once with their browser Cookie. Per-token credentials are stored,
encrypted at rest, under `data/` (`HABR_MCP_STATE_DIR`). Reading works
anonymously; write and author tools require a logged-in session and report a
clear guard message when the caller is not ready.

## Author layer (drafts)
`create_draft_from_docmost` / `create_draft_from_gdoc` / `get_draft` /
`list_drafts` / `update_draft_from_docmost` / `update_draft_from_gdoc` /
`delete_draft` / `resolve_hubs` / `list_flows` publish
Docmost pages **and Google Docs documents** into Habr **drafts** (`publication/…`,
protocol in `docs/habr-publication-protocol.md`). Promoting a draft to public
status ("Publish") is **NOT implemented** — protocol gap §8. Docmost article
bodies are converted from Docmost ProseMirror to a Habr editorVersion-2 tree
(`src/converter.py`); the `*_from_gdoc` tools first convert the Google Docs API
"Document" JSON to an intermediate Docmost-shaped tree (`src/gdoc_converter.py`)
and then reuse the same pipeline. Images are first downloaded from their source
(Docmost-hosted ones get the `DOCMOST_API_TOKEN`; Google `contentUri` and other
external URLs are fetched WITHOUT it) and re-uploaded to habrastorage; an image
failure does not abort publication (text goes through, unresolved images are
dropped with a warning).

## Setup
All routine actions go through the `Makefile` — run `make help` to list targets.
```bash
make install           # create .venv and install dev/test deps
cp .env.example .env   # then fill in the values  (shortcut: make env)
```

## Running tests
```bash
make test              # runs .venv/bin/pytest
```

## Running the app
```bash
make run               # runs .venv/bin/python main.py
```

## Conventions
- All mutable state goes under `data/` (the per-token credential store).
- All config comes from ENV / `.env` (see `.env.example`), read through
  `Settings`; missing/invalid env var → fail at startup with a readable message.
- No global Habr credentials in code or env; per-user creds arrive via
  `habr_login` and are stored encrypted under `data/`.
- Code comments are in English; MCP tool descriptions (`description=`) are in
  Russian (text for the LLM).
- The six "social" tools (`search_articles`, `list_articles`, `get_comments`,
  `post_comment`, `vote_article`, `vote_comment`) are hidden behind the
  `HABR_MCP_ENABLE_SOCIAL_TOOLS` feature toggle (default **off**); `get_article`
  and the author/draft tools are always exposed.
- All repeated actions (env setup, tests, run) go through `make` targets.
- Python always runs inside a local `.venv`, created automatically by `make` on
  first use — never the system Python.
- Tests are required for new code; in CI `build` depends on `test`.
- No `EXPOSE` in the Dockerfile — Traefik publishes the service via compose
  labels (MCP clients connect to `https://<host>/mcp`).
- Write endpoints are reverse-engineered from Habr's internal API; URL/body/header
  logic is centralised in `src/client.py` — fix it there if Habr changes routes.
