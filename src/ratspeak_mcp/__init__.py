"""ratspeak-mcp: stdio MCP connector for Ratspeak / LXMF."""

from ratspeak_mcp.server import run


def main() -> None:
    run()


__all__ = ["main", "run"]
