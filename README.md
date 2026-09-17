# ratspeak-mcp

Local **stdio MCP connector** for [Ratspeak](https://ratspeak.org/) (Reticulum / LXMF mesh messaging).

Keeps a Reticulum + LXMF presence on this machine and exposes tools so an agent (e.g. Grok Bot “Rat”) can check status, announce, send text messages, and read the inbox.

**Private keys are never returned by tools, never printed in the README examples, and never written to stdout.**

## Requirements

- Python 3.11+
- Network access to a Reticulum TCP hub (default: Emerald `2.ratspeak.org:4242`)

## Install / run

```bash
cd /workspace/ratspeak-mcp
uv sync
uv run ratspeak-mcp
```

Or after `uv sync`, use the venv entry point directly:

```bash
/workspace/ratspeak-mcp/.venv/bin/ratspeak-mcp
```

On first run the server will:

1. Create data dir `/home/box/.ratspeak-mcp` (override with `RATSPEAK_MCP_HOME`)
2. Generate an LXMF identity (private key file under `identity/`, mode `0600`)
3. Write Reticulum config with TCP client → `2.ratspeak.org:4242`
4. Announce the delivery destination
5. Write the public destination hash to `destination_hash.txt`

## Data directory

| Path | Purpose |
|------|---------|
| `$RATSPEAK_MCP_HOME/` | Root (default `/home/box/.ratspeak-mcp`) |
| `identity/lxmf_identity` | Private identity (keep secret) |
| `rns/config` | Reticulum config (TCP Emerald) |
| `lxmf/` | LXMF router storage |
| `inbox.json` | Local message cache |
| `destination_hash.txt` | Public 32-hex destination hash |

Environment:

- `RATSPEAK_MCP_HOME` — data directory
- `RATSPEAK_MCP_DISPLAY_NAME` — LXMF display name (default `Rat`)
- `RATSPEAK_MCP_TCP_HOST` / `RATSPEAK_MCP_TCP_PORT` — hub (default Emerald)

Public Ratspeak TCP nodes:

- Emerald: `2.ratspeak.org:4242` (default)
- Ruby: `1.ratspeak.org:4141`
- Diamond: `3.ratspeak.org:4343`

## MCP tools

| Tool | Description |
|------|-------------|
| `ratspeak_status` | Presence, TCP online, interfaces, inbox count |
| `ratspeak_identity` | Destination hash only (no private keys) |
| `ratspeak_announce` | Announce LXMF delivery destination |
| `ratspeak_send` | Send text message (`dest_hash`, `body`, optional `subject`) |
| `ratspeak_inbox` | Recent messages |
| `ratspeak_read` | One message by `message_id` |

## Add as Cursor MCP server

Use a stdio server pointing at the installed entry point:

**Command:** `/workspace/ratspeak-mcp/.venv/bin/ratspeak-mcp`

**Args:** _(none)_

**cwd (optional):** `/workspace/ratspeak-mcp`

Equivalent `mcp.json` / AddMcpServer style:

```json
{
  "mcpServers": {
    "ratspeak": {
      "command": "/workspace/ratspeak-mcp/.venv/bin/ratspeak-mcp",
      "args": []
    }
  }
}
```

Or via `uv`:

```json
{
  "mcpServers": {
    "ratspeak": {
      "command": "uv",
      "args": ["run", "--directory", "/workspace/ratspeak-mcp", "ratspeak-mcp"]
    }
  }
}
```

## Smoke test

```bash
cd /workspace/ratspeak-mcp
uv run python scripts/smoke_test.py
```

## Notes

- Text messages only in this prototype.
- Direct delivery needs a live mesh path to the peer (peer must have announced).
- Reticulum logs go to `$RATSPEAK_MCP_HOME/rns/logfile` so MCP stdio stays clean.
