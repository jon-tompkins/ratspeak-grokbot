"""MCP stdio server exposing Ratspeak / LXMF tools."""

from __future__ import annotations

import json
from typing import Any

from mcp.server import MCPServer

from ratspeak_mcp.bridge import get_bridge
from ratspeak_mcp.send_ipc import ipc_available, send_via_ipc

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
        "Call ratspeak_announce after long idle periods so peers can find you. "
        "When ratspeak-bridge is running, ratspeak_send prefers the local send.sock IPC "
        "so outbound works without stopping the daemon."
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

    Prefers the always-on daemon Unix IPC (`send.sock`) when present so the
    primary RNS holder can send without stopping. Falls back to an in-process
    bridge only if IPC is unavailable.

    Args:
        dest_hash: 32-character hex LXMF destination hash of the recipient.
        body: Message text body.
        subject: Optional subject/title.
    """
    if ipc_available():
        try:
            result = send_via_ipc(
                dest_hash=dest_hash,
                body=body,
                subject=subject or None,
            )
            return _json(result)
        except Exception as e:
            # Soft-fallback to in-process if IPC exists but fails unexpectedly.
            # (Often still fails with share_instance secondary — surface error.)
            try:
                bridge = get_bridge()
                result = bridge.send(
                    dest_hash=dest_hash, body=body, subject=subject or None
                )
                return _json(result)
            except Exception:
                return _json({"ok": False, "error": f"IPC send failed: {e}"})

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
    # When the always-on daemon holds primary RNS, this may attach as shared
    # instance; outbound send still prefers send.sock IPC when available.
    get_bridge()
    mcp.run(transport="stdio")
