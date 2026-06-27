"""Thin entry point: build the Habr MCP server and run it over HTTP.

HTTP-only (streamable-http) so the server can serve many users at once; identity
comes from each request's ``Authorization: Bearer <token>`` header.
"""

from src.server import build_server


def main() -> None:
    server = build_server()
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
