"""Thin entry point: build the Habr MCP server and run it over stdio."""

from src.server import build_server


def main() -> None:
    server = build_server()
    server.run()  # stdio transport (default)


if __name__ == "__main__":
    main()
