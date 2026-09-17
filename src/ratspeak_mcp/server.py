"""MCP stdio server exposing Ratspeak / LXMF tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer

from ratspeak_mcp.bridge import get_bridge

mcp = MCPServer(
    "ratspeak-mcp",
    title="Ratspeak / LXMF",
    description=(
        "Local Reticulum+LXMF presence for Ratspeak mesh messaging. "
        "Tools never expose private keys — only destination hashes."
    ),
    instructions=(
        "Use ratspeak_status / ratspeak_identity to learn this node's destination hash. "
        "Share that hash with peers on Ratspeak. Use ratspeak_send to message a peer "
        "destination hash, and ratspeak_inbox / ratspeak_read for received messages. "
        "Call ratspeak_announce after long idle periods so peers can find you."
    ),
)


def _json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


@mcp.tool()
def ratspeak_status() -> str:
    """Return Ratspeak/LXMF presence status: destination hash, TCP online, interfaces, inbox count."""
    bridge = get_bridge()
    return _json(bridge.status())


@mcp.tool()
def ratspeak_identity() -> str:
    """Return public identity info (destination hash only). Never returns private keys."""
    bridge = get_bridge()
    return _json(bridge.identity_info())


@mcp.tool()
def ratspeak_announce() -> str:
    """Announce this LXMF delivery destination on the Reticulum mesh so peers can find it."""
    bridge = get_bridge()
    return _json(bridge.announce())


@mcp.tool()
def ratspeak_send(dest_hash: str, body: str, subject: str = "") -> str:
    """Send a text LXMF message to a destination hash.

    Args:
        dest_hash: 32-character hex LXMF destination hash of the recipient.
        body: Message text body.
        subject: Optional subject/title.
    """
    bridge = get_bridge()
    result = bridge.send(dest_hash=dest_hash, body=body, subject=subject or None)
    return _json(result)


@mcp.tool()
def ratspeak_inbox(limit: int = 20) -> str:
    """List recent inbound/outbound messages (newest first).

    Args:
        limit: Max number of messages to return (default 20).
    """
    bridge = get_bridge()
    messages = bridge.inbox(limit=limit)
    return _json({"messages": messages, "count": len(messages)})


@mcp.tool()
def ratspeak_read(message_id: str) -> str:
    """Read one stored message by message_id from the local inbox.

    Args:
        message_id: Message id returned by ratspeak_inbox or ratspeak_send.
    """
    bridge = get_bridge()
    return _json(bridge.read(message_id))


def run() -> None:
    # Eager-start bridge so identity/hash exist even before first tool call.
    get_bridge()
    mcp.run(transport="stdio")
