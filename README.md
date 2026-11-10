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
| `send.sock` | Unix IPC for outbound send while daemon holds RNS (mode `0600`) |
| `webhook_fired.json` | Debounce set of successfully delivered webhook message ids |
| `webhook_queue.json` | On-disk retry queue for webhook deliveries not yet 2xx |
| `webhook_key.txt` | Optional Bearer token file (mode `0600`; never commit) |
| `watchdog-state.json` | Local health-check state for `scripts/ratspeak_watchdog.py` |

Environment:

- `RATSPEAK_MCP_HOME` — data directory
- `RATSPEAK_MCP_DISPLAY_NAME` — LXMF display name (default `Rat`)
- `RATSPEAK_MCP_TCP_HOST` / `RATSPEAK_MCP_TCP_PORT` — hub (default Emerald)
- `RATSPEAK_WEBHOOK_URL` — optional HTTP endpoint for inbound wake POSTs (if unset, daemon still runs for presence but never POSTs)
- `RATSPEAK_WEBHOOK_KEY` — optional Bearer token (`Authorization: Bearer …`)
- `RATSPEAK_WEBHOOK_KEY_FILE` — optional path to a file containing the Bearer token (default `$RATSPEAK_MCP_HOME/webhook_key.txt`)
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
| `ratspeak_send` | Send text message (`dest_hash`, `body`, optional `subject`); prefers daemon `send.sock` IPC when present |
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
- Serves local send IPC at `$RATSPEAK_MCP_HOME/send.sock` (see below)
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

`content_preview` is truncated to ~200 characters. Auth header `Authorization: Bearer ${RATSPEAK_WEBHOOK_KEY}` is sent when the key env (or `webhook_key.txt`) is set. HTTP failures soft-fail (mesh loop never crashes): the payload is persisted in `webhook_queue.json` and retried with exponential backoff (1s, 2s, 4s, … capped at 15 minutes) until a 2xx response. Only then is the `message_id` recorded in `webhook_fired.json`. Each attempt is logged with `message_id`, attempt number, and a secret-free reason (for example `HTTP 401` or `URLError`) — never the URL, key, or Authorization header.

The daemon drains the queue on startup and on its heartbeat. You can also retry on demand (no mesh traffic):

```bash
ratspeak-webhook --status
ratspeak-webhook --retry
# or force all items, ignoring backoff:
ratspeak-webhook --force
```

The send IPC also accepts `{"op": "webhook_retry"}` and includes `webhook_pending`, `webhook_url_set`, and `webhook_key_set` (booleans only) in `{"op": "status"}`.

**Allowlist default:** if `RATSPEAK_WEBHOOK_ALLOWLIST` is unset, only source hash `60c8a4197d1de58a53f3a95a1c97f4af` triggers webhooks (this deploy). Set the env to a comma-separated list to override.

### Local send IPC (outbound without stopping the daemon)

While `ratspeak-bridge` holds the primary Reticulum instance, a second process (MCP stdio) often cannot send and may report **Not connected**. The daemon therefore listens on a Unix domain socket:

- Path: `$RATSPEAK_MCP_HOME/send.sock` (mode `0600`)
- Protocol: **newline-delimited JSON** (one request → one response)

Request examples:

```json
{"op": "ping"}
{"op": "status"}
{"op": "send", "dest_hash": "<32 hex>", "body": "hello", "subject": ""}
```

`send` responses mirror `RatspeakBridge.send`: `ok`, `message_id`, `dest_hash`, `subject`, `method`, `lxmf_state`, `delivered` (plus `error` on failure). Private keys are never logged or returned.

CLI (prints JSON on stdout):

```bash
# Ping / status (no mesh traffic)
ratspeak-send --ping
ratspeak-send --status
# or:
uv run python -m ratspeak_mcp.send_cli --ping

# Send via the running daemon
ratspeak-send 60c8a4197d1de58a53f3a95a1c97f4af "hello from IPC"
ratspeak-send --subject "hi" DEST_HASH "body text"
```

MCP tool `ratspeak_send` **prefers this IPC** when `send.sock` exists, and only falls back to an in-process bridge if the socket is unavailable.

### Zero-inference watchdog

`scripts/ratspeak_watchdog.py` is a small looping process (no model, no LXMF traffic to peers). About every 5 minutes it:

1. Checks that a `ratspeak-bridge` process is running, `send.sock` exists, and IPC `ping` / `status` succeed
2. Retries any due items in `webhook_queue.json`
3. If the bridge is unhealthy on two consecutive checks, performs a clean restart: `pkill` the bridge, wait until `pgrep` is empty, `rm -f send.sock`, then run the start script

It does **not** send test messages to the owner's address. It also does not send a self-addressed loopback by default — a silent stall where the process and IPC still respond would not be detected. Interface `tcp_online` flaps are logged but do not trigger a restart on their own.

There is typically no `cron` on the Grok Bot box, and a user systemd bus may be unavailable. Run the watchdog as a background loop and start it from your bridge start script so it comes back after a box restart (it does not survive a box restart by itself):

```bash
nohup /workspace/ratspeak-mcp/.venv/bin/python /workspace/ratspeak-mcp/scripts/ratspeak_watchdog.py --loop --interval 300 >>/tmp/ratspeak-watchdog.out 2>&1 &
```

A one-shot check (no restart) is `python scripts/ratspeak_watchdog.py --no-restart`.

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
uv run python -m unittest tests.test_webhook tests.test_send_ipc -v
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
