"""Configuration for the Habr MCP server.

The server is HTTP-only and multi-tenant: per-user Habr credentials are NOT read
from the global environment. The credential fields below survive only as a
per-user ``Settings`` carrier that the client registry fills from the per-token
credential store. The global env only configures shared, non-secret options
(host/port/state dir, language, proxy, timeout, user agent, page size).
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_errors import load_settings_or_exit

# Realistic desktop Chrome UA so Habr's API treats us like a normal browser.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class Settings(BaseSettings):
    """Server-level settings, populated from env / ``.env``.

    These are the shared, non-secret options (HTTP bind, state dir, language,
    proxy, timeouts). Per-user Habr credentials are NOT read from the global
    env in the multi-tenant model — they are supplied per token via the
    ``habr_login`` tool and carried on a per-user ``Settings`` copy by the
    registry. The credential fields below exist only as that carrier.
    """

    # HTTP transport bind address and port (env HABR_MCP_HOST / HABR_MCP_PORT).
    host: str = Field(default="127.0.0.1", validation_alias="HABR_MCP_HOST")
    port: int = Field(default=8765, validation_alias="HABR_MCP_PORT")
    # Directory holding the per-token credential store (env HABR_MCP_STATE_DIR).
    # All mutable state lives under data/ so it maps to the docker volume.
    state_dir: str = Field(default="data", validation_alias="HABR_MCP_STATE_DIR")
    # Content/flow language (`fl`) and interface language (`hl`); Habr uses both.
    habr_lang: str = "ru"
    # Per-user carrier fields. These are NO LONGER read from the global env for
    # auth: the client registry builds a per-user Settings and fills them from
    # the per-token credential store. They stay so HabrClient keeps its shape.
    # `connect.sid` cookie value from a logged-in browser session (write auth).
    habr_connect_sid: str | None = None
    # CSRF token: the value sent in the `csrf-token` request header (write auth).
    habr_csrf_token: str | None = None
    # Name of the CSRF cookie that must echo the token (double-submit cookie).
    habr_csrf_cookie_name: str = "csrf_token"
    # FULL Cookie header string for author endpoints (publication/…). Protocol §2
    # says a single connect.sid is not enough — the session is held by the bundle
    # connect_sid + hsec_id + habrsession_id + …, so store the whole Cookie header.
    habr_cookie: str | None = None
    # Value for the `habr-user-uuid` request header (mirrors the habr_uuid cookie).
    habr_user_uuid: str | None = None
    # Value for the `x-app-version` request header (Habr frontend version).
    habr_x_app_version: str = "2.329.0"
    # Base URL to resolve & download Docmost image attachments (image reupload only).
    docmost_base_url: str | None = None
    # Bearer token to download Docmost attachments (image reupload only).
    docmost_api_token: str | None = None
    # httpx request timeout in seconds.
    request_timeout: float = 20.0
    # Optional HTTP/SOCKS proxy URL passed straight to httpx.
    proxy: str | None = None
    # Browser-like User-Agent header.
    user_agent: str = DEFAULT_USER_AGENT
    # Page size for feeds / search.
    per_page: int = 20
    # Feature toggle: expose the "social" Habr tools (feed/search browsing and
    # comment/vote interaction). Disabled by default; enable with
    # HABR_MCP_ENABLE_SOCIAL_TOOLS=true. Gates exactly six tools:
    # search_articles, list_articles, get_comments, post_comment,
    # vote_article, vote_comment.
    enable_social_tools: bool = Field(
        default=False, validation_alias="HABR_MCP_ENABLE_SOCIAL_TOOLS"
    )

    # ``populate_by_name`` lets callers/tests pass ``host``/``port``/``state_dir``
    # by field name even though those read from ``HABR_MCP_*`` env via aliases.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True
    )


# Module-level singleton: build settings via the standard helper so a future
# required env var gives a clean message instead of a raw pydantic traceback.
# With no required fields today this never fails, but it is the standard pattern.
settings = load_settings_or_exit(Settings)
