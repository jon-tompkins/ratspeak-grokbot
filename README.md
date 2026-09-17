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
| `daemon.log` | Always-on bridge file log |
| `webhook_fired.json` | Debounce set of webhook-fired message ids |

Environment:

- `RATSPEAK_MCP_HOME` — data directory
- `RATSPEAK_MCP_DISPLAY_NAME` — LXMF display name (default `Rat`)
- `RATSPEAK_MCP_TCP_HOST` / `RATSPEAK_MCP_TCP_PORT` — hub (default Emerald)
- `RATSPEAK_WEBHOOK_URL` — optional HTTP endpoint for inbound wake POSTs (if unset, daemon still runs for presence but never POSTs)
- `RATSPEAK_WEBHOOK_KEY` — optional Bearer token (`Authorization: Bearer …`)
- `RATSPEAK_WEBHOOK_ALLOWLIST` — comma-separated source destination hashes; if unset, defaults to `60c8a4197d1de58a53f3a95a1c97f4af` for this deploy

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


## Always-on bridge + webhook wake

The MCP stdio server only lives while the agent session is open. For continuous mesh presence (and optional HTTP wake-ups when allowlisted peers message you), run the **ratspeak-bridge** daemon.

### Launch

```bash
cd /workspace/ratspeak-mcp
uv sync
uv run ratspeak-bridge
# or:
/workspace/ratspeak-mcp/.venv/bin/ratspeak-bridge
```

Behavior:

- Starts `RatspeakBridge` (same identity / data home as the MCP tools)
- Stays alive forever; re-announces about every **25 minutes**
- Logs to `$RATSPEAK_MCP_HOME/daemon.log` (and minimal status on stdout — **never private keys**)
- On inbound delivery: if `source_hash` is allowlisted **and** `RATSPEAK_WEBHOOK_URL` is set, POSTs JSON:

```json
{
  "event": "ratspeak.inbound",
  "message_id": "...",
  "source_hash": "...",
  "destination_hash": "...",
  "title": "...",
  "content_preview": "...",
  "timestamp": 0
}
```

`content_preview` is truncated to ~200 characters. Auth header `Authorization: Bearer ${RATSPEAK_WEBHOOK_KEY}` is sent when the key env is set. HTTP failures soft-fail (mesh loop never crashes). Fired `message_id`s are debounced in `webhook_fired.json`.

**Allowlist default:** if `RATSPEAK_WEBHOOK_ALLOWLIST` is unset, only source hash `60c8a4197d1de58a53f3a95a1c97f4af` triggers webhooks (this deploy). Set the env to a comma-separated list to override.

### systemd-ish example

```ini
[Unit]
Description=Ratspeak always-on bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/workspace/ratspeak-mcp
ExecStart=/workspace/ratspeak-mcp/.venv/bin/ratspeak-bridge
Restart=always
RestartSec=5
Environment=RATSPEAK_MCP_HOME=/home/box/.ratspeak-mcp
Environment=RATSPEAK_MCP_DISPLAY_NAME=Rat
# Environment=RATSPEAK_WEBHOOK_URL=https://example.com/wake
# Environment=RATSPEAK_WEBHOOK_KEY=replace-me
# Environment=RATSPEAK_WEBHOOK_ALLOWLIST=60c8a4197d1de58a53f3a95a1c97f4af

[Install]
WantedBy=multi-user.target
```

Or under a process supervisor / `nohup`:

```bash
nohup /workspace/ratspeak-mcp/.venv/bin/ratspeak-bridge >>/tmp/ratspeak-bridge.out 2>&1 &
```

### Unit tests

```bash
cd /workspace/ratspeak-mcp
uv run python -m unittest tests.test_webhook -v
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
