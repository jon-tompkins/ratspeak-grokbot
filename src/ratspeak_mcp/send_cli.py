"""CLI client for daemon send IPC: ratspeak-send / python -m ratspeak_mcp.send_cli."""

from __future__ import annotations

import argparse
import json
import sys

from ratspeak_mcp.send_ipc import ipc_available, ipc_call, send_sock_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ratspeak-send",
        description=(
            "Send an LXMF text message via the always-on ratspeak-bridge IPC "
            "(Unix socket under $RATSPEAK_MCP_HOME/send.sock)."
        ),
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Ping the daemon IPC (no mesh send)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Ask the daemon for status via IPC",
    )
    parser.add_argument("--subject", "-s", default="", help="Optional subject/title")
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="IPC / delivery timeout seconds (default 120)",
    )
    parser.add_argument(
        "dest_hash",
        nargs="?",
        help="32-hex destination hash (required unless --ping/--status)",
    )
    parser.add_argument(
        "body",
        nargs="?",
        help="Message body text (required unless --ping/--status)",
    )

    args = parser.parse_args(argv)

    sock = send_sock_path()
    if not ipc_available():
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"IPC socket unavailable: {sock} (is ratspeak-bridge running?)",
                }
            ),
            file=sys.stdout,
        )
        return 1

    try:
        if args.ping:
            resp = ipc_call({"op": "ping"}, timeout=args.timeout)
        elif args.status:
            resp = ipc_call({"op": "status"}, timeout=args.timeout)
        elif args.dest_hash is not None and args.body is not None:
            req = {
                "op": "send",
                "dest_hash": args.dest_hash,
                "body": args.body,
                "subject": args.subject or "",
            }
            resp = ipc_call(req, timeout=args.timeout)
        else:
            parser.error("provide DEST_HASH BODY, or --ping / --status")
            return 2
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stdout)
        return 1

    print(json.dumps(resp, indent=2, default=str))
    if args.ping:
        return 0 if resp.get("ok") and resp.get("pong") else 1
    if args.status:
        return 0 if resp.get("ok") else 1
    return 0 if resp.get("ok") or resp.get("delivered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
