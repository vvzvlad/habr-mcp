"""Configuration for the Habr MCP server.

All values are read from the environment (or a local ``.env`` file). Read tools
work anonymously, so credentials are optional; only write tools require
``habr_connect_sid`` + ``habr_csrf_token``.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

# Realistic desktop Chrome UA so Habr's API treats us like a normal browser.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class Settings(BaseSettings):
    """Server settings, populated from env / ``.env``.

    Env var names map to field names case-insensitively, e.g. the field
    ``habr_connect_sid`` is filled from ``HABR_CONNECT_SID``.
    """

    # Content/flow language (`fl`) and interface language (`hl`); Habr uses both.
    habr_lang: str = "ru"
    # `connect.sid` cookie value from a logged-in browser session (write auth).
    habr_connect_sid: str | None = None
    # CSRF token: the value sent in the `csrf-token` request header (write auth).
    habr_csrf_token: str | None = None
    # Name of the CSRF cookie that must echo the token (double-submit cookie).
    habr_csrf_cookie_name: str = "csrf_token"
    # httpx request timeout in seconds.
    request_timeout: float = 20.0
    # Optional HTTP/SOCKS proxy URL passed straight to httpx.
    proxy: str | None = None
    # Browser-like User-Agent header.
    user_agent: str = DEFAULT_USER_AGENT
    # Page size for feeds / search.
    per_page: int = 20

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
