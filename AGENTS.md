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
  of articles/drafts).
- `src/converter.py` — pure Docmost (TipTap) ProseMirror → Habr editorVersion-2
  converter (for the author/draft tools).
- `src/gdoc_converter.py` — pure Google Docs API "Document" JSON → intermediate
  Docmost-shaped (TipTap) doc; its output then feeds `src/converter.py`.
- `src/server.py` — `build_server()`: registers MCP tools (read, the
  author/draft layer).
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
`delete_draft` / `resolve_hubs` / `search_hubs` / `list_flows` publish
Docmost pages **and Google Docs documents** into Habr **drafts** (`publication/…`,
protocol in `docs/habr-publication-protocol.md`). Promoting a draft to public
status ("Publish") is **NOT implemented** — protocol gap §8. Docmost article
bodies are converted from Docmost ProseMirror to a Habr editorVersion-2 tree
(`src/converter.py`); the `*_from_gdoc` tools first convert the Google Docs API
"Document" JSON to an intermediate Docmost-shaped tree (`src/gdoc_converter.py`)
and then reuse the same pipeline. Each image `attrs.src` may be a plain `http(s)`
URL, a `data:` URI, or an MCP `resource_link`; habr resolves it to bytes via
`HabrClient.fetch_resource` (HTTP GET with **no** Authorization header, or a
local `data:` decode) and re-uploads them to habrastorage. There is no Docmost
token — the producer hands habr a fetchable URL / data-URI / resource_link.
Content loss aborts the save: if any image/drawio/excalidraw ends up without a
habrastorage URL (fetch or upload failed, no `src`) or the body has an
unsupported block atom (anything but the harmless `pageBreak`), no draft is
created or updated and the tool returns a Russian error listing each lost node
(type, src, count), the failure reasons and what to do.

## Setup and tests
All routine actions go through the `Makefile` — `make help` lists targets
(`install`, `env`, `test`, `run`, `clean`); `make` creates a local `.venv` on first use.
Tests run in CI: `.gitea/workflows/tests.yml` on PRs, and the `test` job in
`.gitea/workflows/image-check-publish.yml` gates the image build on `main`. The suite
needs no secrets (every `Settings` field has a default); httpx is mocked via respx.

## Conventions
- All mutable state goes under `data/` (the per-token credential store).
- All config comes from ENV / `.env` (see `.env.example`), read through
  `Settings`; missing/invalid env var → fail at startup with a readable message.
- No global Habr credentials in code or env; per-user creds arrive via
  `habr_login` and are stored encrypted under `data/`.
- Code comments are in English; MCP tool descriptions (`description=`) are in
  Russian (text for the LLM).
- Python runs inside the local `.venv`, never the system Python.
- Tests are required for new code; in CI `build` depends on `test`.
- No `EXPOSE` in the Dockerfile — Traefik publishes the service via compose
  labels (MCP clients connect to `https://<host>/mcp`).
- Write endpoints are reverse-engineered from Habr's internal API; URL/body/header
  logic is centralised in `src/client.py` — fix it there if Habr changes routes.

## Limits for agents
- Tests: at most three times the code (`wc -l` of `tests/` against the code). Over the limit,
  still write the test you need, and in the same change go through the old tests: drop or merge
  duplicates and tests of implementation rather than behaviour.
- Comments explain the code as it is and why it is built this way. History ("used to", "after
  the review", "since the incident") goes into the commit message.
- This file stays under ~150 lines: only what an agent would get wrong without it. No decision
  history, incident logs or retelling of the code.
